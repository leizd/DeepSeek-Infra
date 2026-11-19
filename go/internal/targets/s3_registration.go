package targets

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/policy"
)

var ErrS3RegistrationUnavailable = errors.New("S3_TARGET_REGISTRATION_UNAVAILABLE")

// S3Placement describes a deployment's configured worker, excluding credentials.
type S3Placement struct {
	Endpoint, Region, Bucket, Prefix string
}

func (p S3Placement) Fingerprint() []byte {
	hash := sha256.New()
	hash.Write([]byte("deepseek-infra:s3-target-v1\x00"))
	for _, value := range []string{strings.TrimRight(p.Endpoint, "/"), p.Region, p.Bucket, p.Prefix} {
		hash.Write(binary.BigEndian.AppendUint64(nil, uint64(len(value))))
		hash.Write([]byte(value))
	}
	return hash.Sum(nil)
}

// S3Registration contains the public, secret-free registration metadata.
type S3Registration struct {
	fields    map[string]any
	placement S3Placement
}

// ConfiguredS3Registration validates an existing namespace registration before
// issuing a read against the operator-configured endpoint. Endpoint, region and
// namespace are all bound again in the worker's signed receipt.
func (i Input) ConfiguredS3Registration(configured S3Placement) (S3Registration, error) {
	kind := i.Kind()
	if kind != "s3" && kind != "s3-compatible" {
		return S3Registration{}, ErrS3RegistrationUnavailable
	}
	field := func(name string) string { return strip(i.value.field(name).strOr()) }
	bucket := field("bucket")
	if bucket == "" {
		return S3Registration{}, &policy.AppError{Message: "S3 bucket is required", Code: "invalid_payload", Status: 400}
	}
	prefix, region, endpoint := strings.Trim(field("prefix"), "/"), field("region"), field("endpointUrl")
	failureDomain, jurisdiction := region, region
	if region == "" {
		failureDomain, jurisdiction = "remote-s3", "unknown"
	}
	fields, err := i.metadata(region, failureDomain, "s3-compatible", jurisdiction)
	if err != nil {
		return S3Registration{}, err
	}
	fields["quotaBytes"] = nil
	if quota := i.value.field("quotaBytes"); quota.kind != "null" {
		fields["quotaBytes"], err = qos(quota, "quotaBytes", 0)
		if err != nil {
			return S3Registration{}, err
		}
	}
	provider, err := s3RegistrationCredentialProvider(i.value.field("credentialProvider"))
	if err != nil {
		return S3Registration{}, err
	}
	probe := true
	for _, value := range i.value.fields {
		if value.key == "probe" {
			probe = value.value.truth()
			break
		}
	}
	if probe || field("expectedBucketOwner") != "" || configured.Endpoint == "" || configured.Region == "" ||
		strings.TrimRight(endpoint, "/") != strings.TrimRight(configured.Endpoint, "/") ||
		(region != "" && region != configured.Region) || bucket != configured.Bucket || prefix != configured.Prefix {
		return S3Registration{}, ErrS3RegistrationUnavailable
	}
	if region == "" {
		fields["region"] = nil
	}
	fields["schemaVersion"], fields["kind"] = 3, "s3"
	fields["bucket"], fields["prefix"], fields["endpointUrl"] = bucket, prefix, endpoint
	fields["expectedBucketOwner"], fields["lastProbe"] = nil, nil
	fields["credentialProvider"] = provider
	return S3Registration{fields: fields, placement: configured}, nil
}

func s3RegistrationCredentialProvider(input node) (map[string]string, error) {
	provider := map[string]string{"type": "aws-default-chain"}
	if input.kind != "object" || len(input.fields) == 0 {
		return provider, nil
	}
	for _, field := range input.fields {
		switch field.key {
		case "accessKey", "accessKeyId", "secretAccessKey", "secret", "sessionToken", "password", "secret_access_key", "access_key_id", "session_token":
			if field.value.kind != "null" && (field.value.kind != "string" || field.value.text != "") {
				return nil, &policy.AppError{Message: "cloud credentials must not be stored in target registry", Code: "invalid_payload", Status: 400}
			}
		case "type":
			if field.value.kind != "string" || (field.value.text != "aws-default-chain" && field.value.text != "environment") {
				return nil, ErrS3RegistrationUnavailable
			}
			provider["type"] = field.value.text
		default:
			return nil, ErrS3RegistrationUnavailable
		}
	}
	return provider, nil
}

func (r S3Registration) Placement() S3Placement { return r.placement }

func (r S3Registration) Document(targetID, createdAt string) (json.RawMessage, error) {
	fields := make(map[string]any, len(r.fields)+2)
	for key, value := range r.fields {
		fields[key] = value
	}
	fields["targetId"], fields["createdAt"] = targetID, createdAt
	return json.Marshal(fields)
}
