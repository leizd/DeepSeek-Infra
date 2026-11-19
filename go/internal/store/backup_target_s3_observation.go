package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

const MaxBackupTargetS3ReceiptBytes = 40 * 1024
const maxBackupTargetS3ObjectBytes = 16 * 1024
const targetS3SignatureDomain = "deepseek-infra:backup-target-s3-observation-v1\x00"

var targetS3Bucket = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`)

func ValidateBackupTargetS3Request(request *actionv1.InspectS3BackupTargetRequest) error {
	if request == nil || len(request.TargetIdentity) != sha256.Size ||
		!validBackupTargetReadScope(request.SchemaVersion, request.Fence, request.OperationId,
			request.WriterFencingToken, request, request.Fence) {
		return ErrTargetRegistrationInvalid
	}
	return nil
}

func s3PrintableAttribute(value string) bool {
	for i := 0; i < len(value); i++ {
		if value[i] < 33 || value[i] > 126 {
			return false
		}
	}
	return true
}

func validS3TargetObject(object *actionv1.S3BackupTargetObjectObservation) bool {
	if object == nil {
		return true
	}
	if !nativeKnown(object) || len(object.Json) < 2 || len(object.Json) > maxBackupTargetS3ObjectBytes {
		return false
	}
	hash := sha256.Sum256(object.Json)
	data := bytes.TrimSpace(object.Json)
	etag := object.Etag
	return len(data) > 0 && data[0] == '{' && utf8.Valid(object.Json) && json.Valid(object.Json) &&
		bytes.Equal(object.Sha256, hash[:]) && len(etag) >= 3 && len(etag) <= 1024 &&
		etag[0] == '"' && etag[len(etag)-1] == '"' && !strings.ContainsRune(etag[1:len(etag)-1], '"') &&
		s3PrintableAttribute(etag) && len(object.VersionId) <= 1024 && s3PrintableAttribute(object.VersionId)
}

// VerifyBackupTargetS3Receipt authenticates a configured provider read. A nil
// object is a current absence observation and never clears a dispatched write.
func VerifyBackupTargetS3Receipt(request *actionv1.InspectS3BackupTargetRequest, receipt *actionv1.S3BackupTargetInspectionReceipt,
	context BackupTargetObservationContext) error {
	if ValidateBackupTargetS3Request(request) != nil || context.Now <= 0 || context.FleetID == "" || context.Environment == "" ||
		receipt == nil || receipt.Observation == nil || receipt.SchemaVersion != 1 || receipt.SignerPublicKey != context.PublicKey ||
		len(receipt.Signature) != ed25519.SignatureSize || proto.Size(receipt) > MaxBackupTargetS3ReceiptBytes {
		return ErrTargetRegistrationInvalid
	}
	o := receipt.Observation
	if o.SchemaVersion != 1 || !nativeKnown(receipt, o, o.Fence) || !proto.Equal(request.Fence, o.Fence) ||
		o.OperationId != request.OperationId || o.WriterFencingToken != request.WriterFencingToken ||
		!bytes.Equal(o.TargetIdentity, request.TargetIdentity) || !targetS3Bucket.MatchString(o.Bucket) ||
		len(o.Prefix) > 1024 || strings.ContainsRune(o.Prefix, '\\') || strings.IndexFunc(o.Prefix, unicode.IsControl) >= 0 ||
		o.FleetId != context.FleetID || o.Environment != context.Environment || o.ObservedAtUnix <= 0 ||
		o.ObservedAtUnix > context.Now || context.Now-o.ObservedAtUnix > 60 ||
		!validS3TargetObject(o.Identity) || !validS3TargetObject(o.Head) {
		return ErrTargetRegistrationInvalid
	}
	key, err := decodeFixedBase64(context.PublicKey, ed25519.PublicKeySize)
	if err != nil {
		return ErrTargetRegistrationInvalid
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(o)
	if err != nil || !ed25519.Verify(key, append([]byte(targetS3SignatureDomain), wire...), receipt.Signature) {
		return ErrTargetRegistrationInvalid
	}
	return nil
}
