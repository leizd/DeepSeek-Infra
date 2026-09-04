package store

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"sort"
	"strings"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

const (
	AuthorityRequestSchema        = "control-authority-request-v1"
	AuthorityRequestSchemaVersion = 1
	authorityRequestAlgorithm     = "Ed25519"
	maxAuthorityRequestBytes      = 16 * 1024
	maxAuthorityRequestLifetime   = 300
	DefaultAuthorityRequestSkew   = 30
)

var authorityRequestDomain = []byte("deepseek-infra:control-authority-request-v1\x00")

var authorityRequestFields = []string{
	"actionId", "digest", "domain", "environment", "executionEpoch", "expiresAt",
	"fencingToken", "fleetId", "issuedAt", "mode", "nonce", "operation", "payload",
	"payloadDigest", "requestId", "revision", "role", "runtime", "schema",
	"schemaVersion", "signature", "signatureAlgorithm", "signerKeyId",
}

var (
	ErrAuthorityRequestInvalid               = errors.New("AUTHORITY_REQUEST_INVALID")
	ErrAuthorityRequestTooLarge              = errors.New("AUTHORITY_REQUEST_TOO_LARGE")
	ErrAuthorityRequestSchemaInvalid         = errors.New("AUTHORITY_REQUEST_SCHEMA_INVALID")
	ErrAuthorityRequestFieldsInvalid         = errors.New("AUTHORITY_REQUEST_FIELDS_INVALID")
	ErrAuthorityRequestCanonicalMismatch     = errors.New("AUTHORITY_REQUEST_CANONICAL_MISMATCH")
	ErrAuthorityRequestDigestMismatch        = errors.New("AUTHORITY_REQUEST_DIGEST_MISMATCH")
	ErrAuthorityRequestPayloadDigestMismatch = errors.New("AUTHORITY_REQUEST_PAYLOAD_DIGEST_MISMATCH")
	ErrAuthorityRequestSignatureInvalid      = errors.New("AUTHORITY_REQUEST_SIGNATURE_INVALID")
	ErrAuthorityRequestExpired               = errors.New("AUTHORITY_REQUEST_EXPIRED")
	ErrAuthorityRequestFutureSkew            = errors.New("AUTHORITY_REQUEST_FUTURE_SKEW")
	ErrAuthorityRequestReplay                = errors.New("AUTHORITY_REQUEST_REPLAY")
	ErrAuthorityRequestNonceReuse            = errors.New("AUTHORITY_REQUEST_NONCE_REUSE")
	ErrAuthorityRequestDomainMismatch        = errors.New("AUTHORITY_REQUEST_DOMAIN_MISMATCH")
	ErrAuthorityRequestFleetMismatch         = errors.New("AUTHORITY_REQUEST_FLEET_MISMATCH")
	ErrAuthorityRequestEnvironmentMismatch   = errors.New("AUTHORITY_REQUEST_ENVIRONMENT_MISMATCH")
	ErrAuthorityRequestRoleMismatch          = errors.New("AUTHORITY_REQUEST_ROLE_MISMATCH")
	ErrAuthorityRequestRuntimeMismatch       = errors.New("AUTHORITY_REQUEST_RUNTIME_MISMATCH")
	ErrAuthorityRequestModeMismatch          = errors.New("AUTHORITY_REQUEST_MODE_MISMATCH")
	ErrAuthorityRequestOperationInvalid      = errors.New("AUTHORITY_REQUEST_OPERATION_INVALID")
	ErrAuthorityRequestStaleFencingToken     = errors.New("AUTHORITY_REQUEST_STALE_FENCING_TOKEN")
	ErrAuthorityRequestSignerMismatch        = errors.New("AUTHORITY_REQUEST_SIGNER_MISMATCH")
	ErrAuthorityRequestSecretDetected        = errors.New("AUTHORITY_REQUEST_SECRET_DETECTED")
)

var (
	fleetIDPattern      = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,127}$`)
	hex64Pattern        = regexp.MustCompile(`^[0-9a-f]{64}$`)
	controlIDPattern    = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	signerKeyIDPattern  = regexp.MustCompile(`^ctrl-signer-[0-9a-f]{16}$`)
	allowedRequestOps   = map[string]bool{"install-epoch": true}
	allowedRequestModes = map[string]bool{ModeShadow: true}
)

type AuthorityRequestContext struct {
	Now                  time.Time
	SignerPublicKey      string
	SignerKeyID          string
	ExpectedDomain       string
	ExpectedOperation    string
	ExpectedRuntime      string
	ExpectedMode         string
	ExpectedFleetID      string
	ExpectedEnvironment  string
	ExpectedRole         string
	CurrentFencingToken  int64
	LiveEpoch            int64
	SeenRequestIDs       map[string]bool
	SeenNonces           map[string]bool
	MaxFutureSkewSeconds int64
}

func VerifyAuthorityRequestDocument(raw []byte, context AuthorityRequestContext) (map[string]any, error) {
	if len(raw) == 0 {
		return nil, ErrAuthorityRequestInvalid
	}
	if len(raw) > maxAuthorityRequestBytes {
		return nil, ErrAuthorityRequestTooLarge
	}
	var document map[string]any
	if err := decodeSingleJSON(raw, &document); err != nil || document == nil {
		return nil, ErrAuthorityRequestInvalid
	}
	canonical, err := canonicalAuthorityJSON(document)
	if err != nil || string(canonical) != string(raw) {
		return nil, ErrAuthorityRequestCanonicalMismatch
	}
	keys := make([]string, 0, len(document))
	for key := range document {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	if len(keys) != len(authorityRequestFields) {
		return nil, ErrAuthorityRequestFieldsInvalid
	}
	for index, key := range keys {
		if key != authorityRequestFields[index] {
			return nil, ErrAuthorityRequestFieldsInvalid
		}
	}
	if err := rejectControlSecretMaterial(document, 0); err != nil {
		if errors.Is(err, ErrSecretDetected) {
			return nil, ErrAuthorityRequestSecretDetected
		}
		return nil, ErrAuthorityRequestInvalid
	}
	if err := verifyAuthorityRequestEnvelope(document, context); err != nil {
		return nil, err
	}
	if err := verifyAuthorityRequestSignature(document, context); err != nil {
		return nil, err
	}
	return document, nil
}

func verifyAuthorityRequestEnvelope(document map[string]any, context AuthorityRequestContext) error {
	if asString(document["schema"]) != AuthorityRequestSchema || asInt(document["schemaVersion"]) != AuthorityRequestSchemaVersion {
		return ErrAuthorityRequestSchemaInvalid
	}
	operation := asString(document["operation"])
	if !allowedRequestOps[operation] || operation != context.ExpectedOperation {
		return ErrAuthorityRequestOperationInvalid
	}
	domain := asString(document["domain"])
	if _, ok := tableForDomain(domain); !ok || domain != context.ExpectedDomain {
		return ErrAuthorityRequestDomainMismatch
	}
	if asString(document["runtime"]) != RuntimeGo || asString(document["runtime"]) != context.ExpectedRuntime {
		return ErrAuthorityRequestRuntimeMismatch
	}
	if !allowedRequestModes[asString(document["mode"])] || asString(document["mode"]) != context.ExpectedMode {
		return ErrAuthorityRequestModeMismatch
	}
	fleetID := asString(document["fleetId"])
	if !fleetIDPattern.MatchString(fleetID) || fleetID != context.ExpectedFleetID {
		return ErrAuthorityRequestFleetMismatch
	}
	environment := asString(document["environment"])
	if environment == "" || environment != context.ExpectedEnvironment {
		return ErrAuthorityRequestEnvironmentMismatch
	}
	if asString(document["role"]) != "control-plane" || asString(document["role"]) != context.ExpectedRole {
		return ErrAuthorityRequestRoleMismatch
	}
	actionID := asString(document["actionId"])
	if !controlIDPattern.MatchString(actionID) {
		return internalprotocol.ErrEmptyActionID
	}
	epoch := asInt(document["executionEpoch"])
	if epoch < 1 {
		return internalprotocol.ErrZeroEpoch
	}
	revision := asInt(document["revision"])
	if revision < 1 {
		return ErrAuthorityRequestInvalid
	}
	fencingToken := asInt(document["fencingToken"])
	if fencingToken < 1 || fencingToken != context.CurrentFencingToken {
		return ErrAuthorityRequestStaleFencingToken
	}
	if epoch <= context.LiveEpoch {
		return internalprotocol.ErrStaleEpoch
	}
	requestID := asString(document["requestId"])
	nonce := asString(document["nonce"])
	if !hex64Pattern.MatchString(requestID) || !hex64Pattern.MatchString(nonce) {
		return ErrAuthorityRequestInvalid
	}
	if context.SeenRequestIDs[requestID] {
		return ErrAuthorityRequestReplay
	}
	if context.SeenNonces[nonce] {
		return ErrAuthorityRequestNonceReuse
	}
	payload, ok := document["payload"].(map[string]any)
	if !ok || len(payload) != 0 {
		return ErrAuthorityRequestInvalid
	}
	expectedPayloadDigest, err := typedDigest(payload)
	if err != nil || asString(document["payloadDigest"]) != expectedPayloadDigest {
		return ErrAuthorityRequestPayloadDigestMismatch
	}
	expectedDigest, err := authorityRequestDigest(document)
	if err != nil || asString(document["digest"]) != expectedDigest {
		return ErrAuthorityRequestDigestMismatch
	}
	issuedAt, err := parseAuthorityTimestamp(asString(document["issuedAt"]))
	if err != nil {
		return err
	}
	expiresAt, err := parseAuthorityTimestamp(asString(document["expiresAt"]))
	if err != nil {
		return err
	}
	now := context.Now.UTC().Truncate(time.Second)
	if !expiresAt.After(issuedAt) || expiresAt.Sub(issuedAt) > maxAuthorityRequestLifetime*time.Second {
		return ErrAuthorityRequestInvalid
	}
	if !expiresAt.After(now) {
		return ErrAuthorityRequestExpired
	}
	skew := context.MaxFutureSkewSeconds
	if skew < 0 {
		skew = 0
	}
	if issuedAt.Sub(now) > time.Duration(skew)*time.Second {
		return ErrAuthorityRequestFutureSkew
	}
	return nil
}

func verifyAuthorityRequestSignature(document map[string]any, context AuthorityRequestContext) error {
	if asString(document["signatureAlgorithm"]) != authorityRequestAlgorithm {
		return ErrAuthorityRequestSignatureInvalid
	}
	signerKeyID := asString(document["signerKeyId"])
	if signerKeyID != context.SignerKeyID || !signerKeyIDPattern.MatchString(signerKeyID) {
		return ErrAuthorityRequestSignerMismatch
	}
	signature, err := decodeFixedBase64(asString(document["signature"]), ed25519.SignatureSize)
	if err != nil {
		return ErrAuthorityRequestSignatureInvalid
	}
	publicKey, err := decodeFixedBase64(context.SignerPublicKey, ed25519.PublicKeySize)
	if err != nil {
		return ErrAuthorityRequestSignatureInvalid
	}
	unsigned := copyWithout(document, "signature")
	canonical, err := canonicalAuthorityJSON(unsigned)
	if err != nil {
		return ErrAuthorityRequestInvalid
	}
	message := append(append([]byte{}, authorityRequestDomain...), canonical...)
	if !ed25519.Verify(publicKey, message, signature) {
		return ErrAuthorityRequestSignatureInvalid
	}
	return nil
}

func authorityRequestDigest(document map[string]any) (string, error) {
	return typedDigest(copyWithout(document, "signature", "digest"))
}

func canonicalAuthorityJSON(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, ErrAuthorityRequestInvalid
	}
	return raw, nil
}

func typedDigest(value any) (string, error) {
	canonical, err := canonicalAuthorityJSON(value)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

func parseAuthorityTimestamp(value string) (time.Time, error) {
	if !strings.HasSuffix(value, "Z") {
		return time.Time{}, ErrAuthorityRequestInvalid
	}
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil || parsed.Nanosecond() != 0 {
		return time.Time{}, ErrAuthorityRequestInvalid
	}
	return parsed.UTC(), nil
}

func decodeFixedBase64(value string, size int) ([]byte, error) {
	if value == "" {
		return nil, ErrAuthorityRequestInvalid
	}
	raw, err := base64.RawURLEncoding.DecodeString(value)
	if err != nil || len(raw) != size {
		return nil, ErrAuthorityRequestInvalid
	}
	return raw, nil
}

func copyWithout(document map[string]any, keys ...string) map[string]any {
	skip := map[string]bool{}
	for _, key := range keys {
		skip[key] = true
	}
	copied := make(map[string]any, len(document))
	for key, value := range document {
		if !skip[key] {
			copied[key] = value
		}
	}
	return copied
}

func asString(value any) string {
	text, _ := value.(string)
	return text
}

func asInt(value any) int64 {
	number, ok := value.(json.Number)
	if !ok {
		return 0
	}
	parsed, err := number.Int64()
	if err != nil {
		return 0
	}
	return parsed
}
