package store

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"sort"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

const (
	MutationRequestSchema        = "control-mutation-request-v1"
	MutationRequestSchemaVersion = 1
	MaxMutationRequestBytes      = 16 * 1024
	maxMutationRequestBytes      = MaxMutationRequestBytes
	maxMutationRequestLifetime   = 300
	DefaultMutationRequestSkew   = 30
	MutationIntentShadowCompare  = "shadow-compare"
	MutationOperationPropose     = "propose-mutation"
)

var mutationRequestDomain = []byte("deepseek-infra:control-mutation-request-v1\x00")

var mutationRequestFields = []string{
	"actionId", "digest", "domain", "environment", "executionEpoch", "expiresAt",
	"fencingToken", "fleetId", "issuedAt", "mode", "nonce", "operation", "operationId",
	"payload", "payloadDigest", "requestId", "revision", "role", "runtime", "schema",
	"schemaVersion", "signature", "signatureAlgorithm", "signerKeyId",
}

var mutationPayloadFields = []string{"intent", "recordId", "revision", "state"}

var (
	ErrMutationRequestInvalid               = errors.New("MUTATION_REQUEST_INVALID")
	ErrMutationRequestTooLarge              = errors.New("MUTATION_REQUEST_TOO_LARGE")
	ErrMutationRequestSchemaInvalid         = errors.New("MUTATION_REQUEST_SCHEMA_INVALID")
	ErrMutationRequestFieldsInvalid         = errors.New("MUTATION_REQUEST_FIELDS_INVALID")
	ErrMutationRequestCanonicalMismatch     = errors.New("MUTATION_REQUEST_CANONICAL_MISMATCH")
	ErrMutationRequestDigestMismatch        = errors.New("MUTATION_REQUEST_DIGEST_MISMATCH")
	ErrMutationRequestPayloadDigestMismatch = errors.New("MUTATION_REQUEST_PAYLOAD_DIGEST_MISMATCH")
	ErrMutationRequestSignatureInvalid      = errors.New("MUTATION_REQUEST_SIGNATURE_INVALID")
	ErrMutationRequestExpired               = errors.New("MUTATION_REQUEST_EXPIRED")
	ErrMutationRequestFutureSkew            = errors.New("MUTATION_REQUEST_FUTURE_SKEW")
	ErrMutationRequestReplay                = errors.New("MUTATION_REQUEST_REPLAY")
	ErrMutationRequestNonceReuse            = errors.New("MUTATION_REQUEST_NONCE_REUSE")
	ErrMutationRequestReplayConflict        = errors.New("MUTATION_REQUEST_REPLAY_CONFLICT")
	ErrMutationRequestDomainMismatch        = errors.New("MUTATION_REQUEST_DOMAIN_MISMATCH")
	ErrMutationRequestFleetMismatch         = errors.New("MUTATION_REQUEST_FLEET_MISMATCH")
	ErrMutationRequestEnvironmentMismatch   = errors.New("MUTATION_REQUEST_ENVIRONMENT_MISMATCH")
	ErrMutationRequestRoleMismatch          = errors.New("MUTATION_REQUEST_ROLE_MISMATCH")
	ErrMutationRequestRuntimeMismatch       = errors.New("MUTATION_REQUEST_RUNTIME_MISMATCH")
	ErrMutationRequestModeMismatch          = errors.New("MUTATION_REQUEST_MODE_MISMATCH")
	ErrMutationRequestOperationInvalid      = errors.New("MUTATION_REQUEST_OPERATION_INVALID")
	ErrMutationRequestStaleFencingToken     = errors.New("MUTATION_REQUEST_STALE_FENCING_TOKEN")
	ErrMutationRequestSignerMismatch        = errors.New("MUTATION_REQUEST_SIGNER_MISMATCH")
	ErrMutationRequestSecretDetected        = errors.New("MUTATION_REQUEST_SECRET_DETECTED")
)

var allowedMutationOps = map[string]bool{MutationOperationPropose: true}

type MutationRequestContext struct {
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

func SignMutationRequest(unsigned map[string]any, privateKey ed25519.PrivateKey, publicKey string) (map[string]any, []byte, error) {
	if unsigned == nil {
		return nil, nil, ErrMutationRequestInvalid
	}
	if _, exists := unsigned["signature"]; exists {
		return nil, nil, ErrMutationRequestInvalid
	}
	if len(privateKey) != ed25519.PrivateKeySize {
		return nil, nil, ErrMutationRequestSignatureInvalid
	}
	payload := copyWithout(unsigned)
	if _, ok := payload["payload"].(map[string]any); !ok {
		return nil, nil, ErrMutationRequestInvalid
	}
	signerKeyID, err := SignerKeyIDForPublicKey(publicKey)
	if err != nil {
		return nil, nil, ErrMutationRequestSignerMismatch
	}
	payload["signerKeyId"] = signerKeyID
	payload["signatureAlgorithm"] = authorityRequestAlgorithm
	payloadDigest, err := typedDigest(payload["payload"])
	if err != nil {
		return nil, nil, err
	}
	payload["payloadDigest"] = payloadDigest
	digest, err := mutationRequestDigest(payload)
	if err != nil {
		return nil, nil, err
	}
	payload["digest"] = digest
	canonical, err := canonicalAuthorityJSON(payload)
	if err != nil {
		return nil, nil, ErrMutationRequestInvalid
	}
	message := append(append([]byte{}, mutationRequestDomain...), canonical...)
	payload["signature"] = base64.RawURLEncoding.EncodeToString(ed25519.Sign(privateKey, message))
	raw, err := canonicalAuthorityJSON(payload)
	if err != nil {
		return nil, nil, ErrMutationRequestInvalid
	}
	return payload, raw, nil
}

func VerifyMutationRequestDocument(raw []byte, context MutationRequestContext) (map[string]any, error) {
	if len(raw) == 0 {
		return nil, ErrMutationRequestInvalid
	}
	if len(raw) > maxMutationRequestBytes {
		return nil, ErrMutationRequestTooLarge
	}
	var document map[string]any
	if err := decodeSingleJSON(raw, &document); err != nil || document == nil {
		return nil, ErrMutationRequestInvalid
	}
	canonical, err := canonicalAuthorityJSON(document)
	if err != nil || string(canonical) != string(raw) {
		return nil, ErrMutationRequestCanonicalMismatch
	}
	keys := make([]string, 0, len(document))
	for key := range document {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	if len(keys) != len(mutationRequestFields) {
		return nil, ErrMutationRequestFieldsInvalid
	}
	for index, key := range keys {
		if key != mutationRequestFields[index] {
			return nil, ErrMutationRequestFieldsInvalid
		}
	}
	if err := rejectControlSecretMaterial(document, 0); err != nil {
		if errors.Is(err, ErrSecretDetected) {
			return nil, ErrMutationRequestSecretDetected
		}
		return nil, ErrMutationRequestInvalid
	}
	if err := verifyMutationRequestEnvelope(document, context); err != nil {
		return nil, err
	}
	if err := verifyMutationRequestSignature(document, context); err != nil {
		return nil, err
	}
	return document, nil
}

func verifyMutationRequestEnvelope(document map[string]any, context MutationRequestContext) error {
	if asString(document["schema"]) != MutationRequestSchema || asInt(document["schemaVersion"]) != MutationRequestSchemaVersion {
		return ErrMutationRequestSchemaInvalid
	}
	operation := asString(document["operation"])
	if !allowedMutationOps[operation] || operation != context.ExpectedOperation {
		return ErrMutationRequestOperationInvalid
	}
	domain := asString(document["domain"])
	if _, ok := tableForDomain(domain); !ok || domain != context.ExpectedDomain {
		return ErrMutationRequestDomainMismatch
	}
	if asString(document["runtime"]) != RuntimeGo || asString(document["runtime"]) != context.ExpectedRuntime {
		return ErrMutationRequestRuntimeMismatch
	}
	if !allowedRequestModes[asString(document["mode"])] || asString(document["mode"]) != context.ExpectedMode {
		return ErrMutationRequestModeMismatch
	}
	fleetID := asString(document["fleetId"])
	if !fleetIDPattern.MatchString(fleetID) || fleetID != context.ExpectedFleetID {
		return ErrMutationRequestFleetMismatch
	}
	environment := asString(document["environment"])
	if environment == "" || environment != context.ExpectedEnvironment {
		return ErrMutationRequestEnvironmentMismatch
	}
	if asString(document["role"]) != "control-plane" || asString(document["role"]) != context.ExpectedRole {
		return ErrMutationRequestRoleMismatch
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
		return ErrMutationRequestInvalid
	}
	fencingToken := asInt(document["fencingToken"])
	if fencingToken < 1 || fencingToken != context.CurrentFencingToken {
		return ErrMutationRequestStaleFencingToken
	}
	if epoch <= context.LiveEpoch {
		return internalprotocol.ErrStaleEpoch
	}
	requestID := asString(document["requestId"])
	nonce := asString(document["nonce"])
	operationID := asString(document["operationId"])
	if !hex64Pattern.MatchString(requestID) || !hex64Pattern.MatchString(nonce) || !hex64Pattern.MatchString(operationID) {
		return ErrMutationRequestInvalid
	}
	if context.SeenRequestIDs[requestID] {
		return ErrMutationRequestReplay
	}
	if context.SeenNonces[nonce] {
		return ErrMutationRequestNonceReuse
	}
	payload, ok := document["payload"].(map[string]any)
	if !ok {
		return ErrMutationRequestInvalid
	}
	payloadKeys := make([]string, 0, len(payload))
	for key := range payload {
		payloadKeys = append(payloadKeys, key)
	}
	sort.Strings(payloadKeys)
	if len(payloadKeys) != len(mutationPayloadFields) {
		return ErrMutationRequestInvalid
	}
	for index, key := range payloadKeys {
		if key != mutationPayloadFields[index] {
			return ErrMutationRequestInvalid
		}
	}
	if asString(payload["intent"]) != MutationIntentShadowCompare {
		return ErrMutationRequestInvalid
	}
	if !controlIDPattern.MatchString(asString(payload["recordId"])) {
		return ErrMutationRequestInvalid
	}
	if asInt(payload["revision"]) < 1 {
		return ErrMutationRequestInvalid
	}
	if asString(payload["state"]) == "" {
		return ErrMutationRequestInvalid
	}
	expectedPayloadDigest, err := typedDigest(payload)
	if err != nil || asString(document["payloadDigest"]) != expectedPayloadDigest {
		return ErrMutationRequestPayloadDigestMismatch
	}
	if seen, exists := context.SeenOperationDigests[operationID]; exists && seen != expectedPayloadDigest {
		return ErrMutationRequestReplayConflict
	}
	expectedDigest, err := mutationRequestDigest(document)
	if err != nil || asString(document["digest"]) != expectedDigest {
		return ErrMutationRequestDigestMismatch
	}
	issuedAt, err := parseAuthorityTimestamp(asString(document["issuedAt"]))
	if err != nil {
		return ErrMutationRequestInvalid
	}
	expiresAt, err := parseAuthorityTimestamp(asString(document["expiresAt"]))
	if err != nil {
		return ErrMutationRequestInvalid
	}
	now := context.Now.UTC().Truncate(time.Second)
	if !expiresAt.After(issuedAt) || expiresAt.Sub(issuedAt) > maxMutationRequestLifetime*time.Second {
		return ErrMutationRequestInvalid
	}
	if !expiresAt.After(now) {
		return ErrMutationRequestExpired
	}
	skew := context.MaxFutureSkewSeconds
	if skew < 0 {
		skew = 0
	}
	if issuedAt.Sub(now) > time.Duration(skew)*time.Second {
		return ErrMutationRequestFutureSkew
	}
	return nil
}

func verifyMutationRequestSignature(document map[string]any, context MutationRequestContext) error {
	if asString(document["signatureAlgorithm"]) != authorityRequestAlgorithm {
		return ErrMutationRequestSignatureInvalid
	}
	signerKeyID := asString(document["signerKeyId"])
	if signerKeyID != context.SignerKeyID || !signerKeyIDPattern.MatchString(signerKeyID) {
		return ErrMutationRequestSignerMismatch
	}
	signature, err := decodeFixedBase64(asString(document["signature"]), ed25519.SignatureSize)
	if err != nil {
		return ErrMutationRequestSignatureInvalid
	}
	publicKey, err := decodeFixedBase64(context.SignerPublicKey, ed25519.PublicKeySize)
	if err != nil {
		return ErrMutationRequestSignatureInvalid
	}
	unsigned := copyWithout(document, "signature")
	canonical, err := canonicalAuthorityJSON(unsigned)
	if err != nil {
		return ErrMutationRequestInvalid
	}
	message := append(append([]byte{}, mutationRequestDomain...), canonical...)
	if !ed25519.Verify(publicKey, message, signature) {
		return ErrMutationRequestSignatureInvalid
	}
	return nil
}

func mutationRequestDigest(document map[string]any) (string, error) {
	return typedDigest(copyWithout(document, "signature", "digest"))
}
