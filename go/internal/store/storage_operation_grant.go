package store

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

const (
	StorageOperationGrantSchema        = "control-storage-operation-grant-v1"
	StorageOperationGrantSchemaVersion = 1
	MaxStorageOperationGrantBytes      = 16 * 1024
	maxStorageOperationGrantBytes      = MaxStorageOperationGrantBytes
	maxStorageOperationGrantLifetime   = 300
	DefaultStorageOperationGrantSkew   = 30
	StorageOperationGrantPut           = "execute-storage-put"
)

// Canonical bytes are sorted JSON with a domain-separated Ed25519 signature.
// Protobuf encoding of StorageMutationRequest is not a signing input.
var storageOperationGrantDomain = []byte("deepseek-infra:control-storage-operation-grant-v1\x00")

var storageOperationGrantFields = []string{
	"actionId", "digest", "domain", "environment", "executionEpoch", "expiresAt",
	"fencingToken", "fleetId", "issuedAt", "mode", "nonce", "operation", "operationId",
	"payload", "payloadDigest", "requestId", "revision", "role", "runtime", "schema",
	"schemaVersion", "signature", "signatureAlgorithm", "signerKeyId",
}

var storageOperationGrantPayloadFields = []string{
	"bucket", "claimRevision", "conditionType", "expectedEtag", "expectedLength",
	"mutationType", "objectDigest", "objectKey", "prefix", "provider", "targetIdentity",
}

var (
	ErrStorageOperationGrantInvalid               = errors.New("STORAGE_OPERATION_GRANT_INVALID")
	ErrStorageOperationGrantTooLarge              = errors.New("STORAGE_OPERATION_GRANT_TOO_LARGE")
	ErrStorageOperationGrantSchemaInvalid         = errors.New("STORAGE_OPERATION_GRANT_SCHEMA_INVALID")
	ErrStorageOperationGrantFieldsInvalid         = errors.New("STORAGE_OPERATION_GRANT_FIELDS_INVALID")
	ErrStorageOperationGrantCanonicalMismatch     = errors.New("STORAGE_OPERATION_GRANT_CANONICAL_MISMATCH")
	ErrStorageOperationGrantDigestMismatch        = errors.New("STORAGE_OPERATION_GRANT_DIGEST_MISMATCH")
	ErrStorageOperationGrantPayloadDigestMismatch = errors.New("STORAGE_OPERATION_GRANT_PAYLOAD_DIGEST_MISMATCH")
	ErrStorageOperationGrantSignatureInvalid      = errors.New("STORAGE_OPERATION_GRANT_SIGNATURE_INVALID")
	ErrStorageOperationGrantExpired               = errors.New("STORAGE_OPERATION_GRANT_EXPIRED")
	ErrStorageOperationGrantFutureSkew            = errors.New("STORAGE_OPERATION_GRANT_FUTURE_SKEW")
	ErrStorageOperationGrantReplay                = errors.New("STORAGE_OPERATION_GRANT_REPLAY")
	ErrStorageOperationGrantNonceReuse            = errors.New("STORAGE_OPERATION_GRANT_NONCE_REUSE")
	ErrStorageOperationGrantReplayConflict        = errors.New("STORAGE_OPERATION_GRANT_REPLAY_CONFLICT")
	ErrStorageOperationGrantDomainMismatch        = errors.New("STORAGE_OPERATION_GRANT_DOMAIN_MISMATCH")
	ErrStorageOperationGrantFleetMismatch         = errors.New("STORAGE_OPERATION_GRANT_FLEET_MISMATCH")
	ErrStorageOperationGrantEnvironmentMismatch   = errors.New("STORAGE_OPERATION_GRANT_ENVIRONMENT_MISMATCH")
	ErrStorageOperationGrantRoleMismatch          = errors.New("STORAGE_OPERATION_GRANT_ROLE_MISMATCH")
	ErrStorageOperationGrantRuntimeMismatch       = errors.New("STORAGE_OPERATION_GRANT_RUNTIME_MISMATCH")
	ErrStorageOperationGrantModeMismatch          = errors.New("STORAGE_OPERATION_GRANT_MODE_MISMATCH")
	ErrStorageOperationGrantOperationInvalid      = errors.New("STORAGE_OPERATION_GRANT_OPERATION_INVALID")
	ErrStorageOperationGrantStaleFencingToken     = errors.New("STORAGE_OPERATION_GRANT_STALE_FENCING_TOKEN")
	ErrStorageOperationGrantSignerMismatch        = errors.New("STORAGE_OPERATION_GRANT_SIGNER_MISMATCH")
	ErrStorageOperationGrantSecretDetected        = errors.New("STORAGE_OPERATION_GRANT_SECRET_DETECTED")
	ErrStorageOperationGrantAuthorityMissing      = errors.New("STORAGE_OPERATION_GRANT_AUTHORITY_MISSING")
	ErrStorageOperationGrantCommandMismatch       = errors.New("STORAGE_OPERATION_GRANT_COMMAND_MISMATCH")
	ErrStorageOperationGrantMissing               = errors.New("STORAGE_OPERATION_GRANT_MISSING")
)

type StorageOperationGrantContext struct {
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
	SeenOperationDigests map[string]string
	MaxFutureSkewSeconds int64
}

// StorageOperationCommand is the RPC command identity a grant must match.
// It is not a Protobuf encoding and contains no payload bytes.
type StorageOperationCommand struct {
	ActionID       string
	ExecutionEpoch uint64
	OperationID    string
	MutationType   string
	Provider       string
	TargetIdentity string
	Bucket         string
	Prefix         string
	ObjectKey      string
	ObjectDigest   string
	ExpectedLength uint64
	ConditionType  string
	ExpectedETag   string
	ClaimRevision  int64
}

func SignStorageOperationGrant(unsigned map[string]any, privateKey ed25519.PrivateKey, publicKey string) (map[string]any, []byte, error) {
	if unsigned == nil {
		return nil, nil, ErrStorageOperationGrantInvalid
	}
	if _, exists := unsigned["signature"]; exists {
		return nil, nil, ErrStorageOperationGrantInvalid
	}
	if len(privateKey) != ed25519.PrivateKeySize {
		return nil, nil, ErrStorageOperationGrantSignatureInvalid
	}
	payload := copyWithout(unsigned)
	if _, ok := payload["payload"].(map[string]any); !ok {
		return nil, nil, ErrStorageOperationGrantInvalid
	}
	signerKeyID, err := SignerKeyIDForPublicKey(publicKey)
	if err != nil {
		return nil, nil, ErrStorageOperationGrantSignerMismatch
	}
	payload["signerKeyId"] = signerKeyID
	payload["signatureAlgorithm"] = authorityRequestAlgorithm
	payloadDigest, err := typedDigest(payload["payload"])
	if err != nil {
		return nil, nil, err
	}
	payload["payloadDigest"] = payloadDigest
	digest, err := storageOperationGrantDigest(payload)
	if err != nil {
		return nil, nil, err
	}
	payload["digest"] = digest
	canonical, err := canonicalAuthorityJSON(payload)
	if err != nil {
		return nil, nil, ErrStorageOperationGrantInvalid
	}
	message := append(append([]byte{}, storageOperationGrantDomain...), canonical...)
	payload["signature"] = base64.RawURLEncoding.EncodeToString(ed25519.Sign(privateKey, message))
	raw, err := canonicalAuthorityJSON(payload)
	if err != nil {
		return nil, nil, ErrStorageOperationGrantInvalid
	}
	return payload, raw, nil
}

func VerifyStorageOperationGrant(raw []byte, context StorageOperationGrantContext) (map[string]any, error) {
	if len(raw) == 0 {
		return nil, ErrStorageOperationGrantInvalid
	}
	if len(raw) > maxStorageOperationGrantBytes {
		return nil, ErrStorageOperationGrantTooLarge
	}
	var document map[string]any
	if err := decodeSingleJSON(raw, &document); err != nil || document == nil {
		return nil, ErrStorageOperationGrantInvalid
	}
	canonical, err := canonicalAuthorityJSON(document)
	if err != nil || string(canonical) != string(raw) {
		return nil, ErrStorageOperationGrantCanonicalMismatch
	}
	keys := make([]string, 0, len(document))
	for key := range document {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	if len(keys) != len(storageOperationGrantFields) {
		return nil, ErrStorageOperationGrantFieldsInvalid
	}
	for index, key := range keys {
		if key != storageOperationGrantFields[index] {
			return nil, ErrStorageOperationGrantFieldsInvalid
		}
	}
	if err := rejectControlSecretMaterial(document, 0); err != nil {
		if errors.Is(err, ErrSecretDetected) {
			return nil, ErrStorageOperationGrantSecretDetected
		}
		return nil, ErrStorageOperationGrantInvalid
	}
	if err := verifyStorageOperationGrantEnvelope(document, context); err != nil {
		return nil, err
	}
	if err := verifyStorageOperationGrantSignature(document, context); err != nil {
		return nil, err
	}
	return document, nil
}

func BindStorageOperationGrant(document map[string]any, command StorageOperationCommand) error {
	payload, ok := document["payload"].(map[string]any)
	if !ok {
		return ErrStorageOperationGrantInvalid
	}
	epoch := asInt(document["executionEpoch"])
	if epoch < 1 || uint64(epoch) != command.ExecutionEpoch || asString(document["actionId"]) != command.ActionID ||
		asString(document["operationId"]) != command.OperationID {
		return ErrStorageOperationGrantCommandMismatch
	}
	if asString(payload["mutationType"]) != command.MutationType || asString(payload["provider"]) != command.Provider ||
		asString(payload["targetIdentity"]) != command.TargetIdentity || asString(payload["bucket"]) != command.Bucket ||
		asString(payload["prefix"]) != command.Prefix || asString(payload["objectKey"]) != command.ObjectKey ||
		asString(payload["objectDigest"]) != command.ObjectDigest || asString(payload["conditionType"]) != command.ConditionType ||
		asString(payload["expectedEtag"]) != command.ExpectedETag {
		return ErrStorageOperationGrantCommandMismatch
	}
	length, ok := asExactInt(payload["expectedLength"])
	if !ok || length < 0 || uint64(length) != command.ExpectedLength {
		return ErrStorageOperationGrantCommandMismatch
	}
	if command.ClaimRevision != 0 && asInt(payload["claimRevision"]) != command.ClaimRevision {
		return ErrStorageOperationGrantCommandMismatch
	}
	return nil
}

func verifyStorageOperationGrantEnvelope(document map[string]any, context StorageOperationGrantContext) error {
	if asString(document["schema"]) != StorageOperationGrantSchema || asInt(document["schemaVersion"]) != StorageOperationGrantSchemaVersion {
		return ErrStorageOperationGrantSchemaInvalid
	}
	operation := asString(document["operation"])
	if operation != StorageOperationGrantPut || operation != context.ExpectedOperation {
		return ErrStorageOperationGrantOperationInvalid
	}
	if asString(document["domain"]) != "action" || asString(document["domain"]) != context.ExpectedDomain {
		return ErrStorageOperationGrantDomainMismatch
	}
	if asString(document["runtime"]) != RuntimeGo || asString(document["runtime"]) != context.ExpectedRuntime {
		return ErrStorageOperationGrantRuntimeMismatch
	}
	if !allowedRequestModes[asString(document["mode"])] || asString(document["mode"]) != context.ExpectedMode {
		return ErrStorageOperationGrantModeMismatch
	}
	fleetID := asString(document["fleetId"])
	if !fleetIDPattern.MatchString(fleetID) || fleetID != context.ExpectedFleetID {
		return ErrStorageOperationGrantFleetMismatch
	}
	environment := asString(document["environment"])
	if environment == "" || environment != context.ExpectedEnvironment {
		return ErrStorageOperationGrantEnvironmentMismatch
	}
	if asString(document["role"]) != "control-plane" || asString(document["role"]) != context.ExpectedRole {
		return ErrStorageOperationGrantRoleMismatch
	}
	actionID := asString(document["actionId"])
	if !controlIDPattern.MatchString(actionID) {
		return internalprotocol.ErrEmptyActionID
	}
	epoch := asInt(document["executionEpoch"])
	if epoch < 1 {
		return internalprotocol.ErrZeroEpoch
	}
	if context.LiveEpoch < 1 {
		return ErrStorageOperationGrantAuthorityMissing
	}
	if epoch != context.LiveEpoch {
		return internalprotocol.ErrFenceMismatch
	}
	if asInt(document["revision"]) < 1 {
		return ErrStorageOperationGrantInvalid
	}
	fencingToken := asInt(document["fencingToken"])
	if fencingToken < 1 || fencingToken != context.CurrentFencingToken {
		return ErrStorageOperationGrantStaleFencingToken
	}
	requestID := asString(document["requestId"])
	nonce := asString(document["nonce"])
	operationID := asString(document["operationId"])
	if !hex64Pattern.MatchString(requestID) || !hex64Pattern.MatchString(nonce) {
		return ErrStorageOperationGrantInvalid
	}
	if strings.TrimSpace(operationID) == "" || len(operationID) > 1024 || strings.ContainsRune(operationID, 0) {
		return ErrStorageOperationGrantInvalid
	}
	if context.SeenRequestIDs[requestID] {
		return ErrStorageOperationGrantReplay
	}
	if context.SeenNonces[nonce] {
		return ErrStorageOperationGrantNonceReuse
	}
	payload, ok := document["payload"].(map[string]any)
	if !ok {
		return ErrStorageOperationGrantInvalid
	}
	payloadKeys := make([]string, 0, len(payload))
	for key := range payload {
		payloadKeys = append(payloadKeys, key)
	}
	sort.Strings(payloadKeys)
	if len(payloadKeys) != len(storageOperationGrantPayloadFields) {
		return ErrStorageOperationGrantInvalid
	}
	for index, key := range payloadKeys {
		if key != storageOperationGrantPayloadFields[index] {
			return ErrStorageOperationGrantInvalid
		}
	}
	if err := verifyStorageOperationGrantPayload(payload); err != nil {
		return err
	}
	expectedPayloadDigest, err := typedDigest(payload)
	if err != nil || asString(document["payloadDigest"]) != expectedPayloadDigest {
		return ErrStorageOperationGrantPayloadDigestMismatch
	}
	if seen, exists := context.SeenOperationDigests[operationID]; exists && seen != expectedPayloadDigest {
		return ErrStorageOperationGrantReplayConflict
	}
	expectedDigest, err := storageOperationGrantDigest(document)
	if err != nil || asString(document["digest"]) != expectedDigest {
		return ErrStorageOperationGrantDigestMismatch
	}
	issuedAt, err := parseAuthorityTimestamp(asString(document["issuedAt"]))
	if err != nil {
		return ErrStorageOperationGrantInvalid
	}
	expiresAt, err := parseAuthorityTimestamp(asString(document["expiresAt"]))
	if err != nil {
		return ErrStorageOperationGrantInvalid
	}
	now := context.Now.UTC().Truncate(time.Second)
	if !expiresAt.After(issuedAt) || expiresAt.Sub(issuedAt) > maxStorageOperationGrantLifetime*time.Second {
		return ErrStorageOperationGrantInvalid
	}
	if !expiresAt.After(now) {
		return ErrStorageOperationGrantExpired
	}
	skew := context.MaxFutureSkewSeconds
	if skew < 0 {
		skew = 0
	}
	if issuedAt.Sub(now) > time.Duration(skew)*time.Second {
		return ErrStorageOperationGrantFutureSkew
	}
	return nil
}

func verifyStorageOperationGrantPayload(payload map[string]any) error {
	if asString(payload["mutationType"]) != "PUT_CHUNK" || asString(payload["provider"]) != "s3" {
		return ErrStorageOperationGrantInvalid
	}
	if !hex64Pattern.MatchString(asString(payload["targetIdentity"])) || !hex64Pattern.MatchString(asString(payload["objectDigest"])) {
		return ErrStorageOperationGrantInvalid
	}
	for _, key := range []string{"bucket", "prefix", "objectKey", "expectedEtag"} {
		value := asString(payload[key])
		if !utf8.ValidString(value) || len(value) > 1024 || strings.ContainsRune(value, 0) {
			return ErrStorageOperationGrantInvalid
		}
	}
	if strings.TrimSpace(asString(payload["bucket"])) == "" || strings.TrimSpace(asString(payload["objectKey"])) == "" {
		return ErrStorageOperationGrantInvalid
	}
	condition := asString(payload["conditionType"])
	etag := asString(payload["expectedEtag"])
	if condition != "CREATE_ONLY" && condition != "IF_MATCH" ||
		condition == "CREATE_ONLY" && etag != "" ||
		condition == "IF_MATCH" && (len(etag) < 2 || !strings.HasPrefix(etag, "\"") || !strings.HasSuffix(etag, "\"") || strings.ContainsAny(etag, "\r\n")) {
		return ErrStorageOperationGrantInvalid
	}
	revision, ok := asExactInt(payload["claimRevision"])
	if !ok || revision < 1 {
		return ErrStorageOperationGrantInvalid
	}
	length, ok := asExactInt(payload["expectedLength"])
	if !ok || length < 0 || uint64(length) > 8*1024*1024 {
		return ErrStorageOperationGrantInvalid
	}
	return nil
}

func verifyStorageOperationGrantSignature(document map[string]any, context StorageOperationGrantContext) error {
	if asString(document["signatureAlgorithm"]) != authorityRequestAlgorithm {
		return ErrStorageOperationGrantSignatureInvalid
	}
	signerKeyID := asString(document["signerKeyId"])
	if signerKeyID != context.SignerKeyID || !signerKeyIDPattern.MatchString(signerKeyID) {
		return ErrStorageOperationGrantSignerMismatch
	}
	signature, err := decodeFixedBase64(asString(document["signature"]), ed25519.SignatureSize)
	if err != nil {
		return ErrStorageOperationGrantSignatureInvalid
	}
	publicKey, err := decodeFixedBase64(context.SignerPublicKey, ed25519.PublicKeySize)
	if err != nil {
		return ErrStorageOperationGrantSignatureInvalid
	}
	unsigned := copyWithout(document, "signature")
	canonical, err := canonicalAuthorityJSON(unsigned)
	if err != nil {
		return ErrStorageOperationGrantInvalid
	}
	message := append(append([]byte{}, storageOperationGrantDomain...), canonical...)
	if !ed25519.Verify(publicKey, message, signature) {
		return ErrStorageOperationGrantSignatureInvalid
	}
	return nil
}

func storageOperationGrantDigest(document map[string]any) (string, error) {
	return typedDigest(copyWithout(document, "signature", "digest"))
}

func asExactInt(value any) (int64, bool) {
	switch typed := value.(type) {
	case json.Number:
		parsed, err := typed.Int64()
		return parsed, err == nil
	case int64:
		return typed, true
	case int:
		return int64(typed), true
	case float64:
		parsed := int64(typed)
		return parsed, float64(parsed) == typed
	default:
		return 0, false
	}
}
