package store

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"regexp"
	"sort"
	"strings"
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
	// MutationRequestV2Schema is the approved versioned revision that carries a
	// record body and the apply-mutation operation. v1 keeps its schema identity,
	// field set, digests and signature domain unchanged.
	MutationRequestV2Schema        = "control-mutation-request-v2"
	MutationRequestV2SchemaVersion = 2
	MutationOperationApply         = "apply-mutation"
	MutationIntentApply            = "apply-mutation"
	// maxRecordPayloadDepth mirrors the Python oracle's recursion bound.
	maxRecordPayloadDepth = 128
)

var mutationRequestDomain = []byte("deepseek-infra:control-mutation-request-v1\x00")

// mutationRequestDomainV2 is deliberately different from v1: the domain
// separator is what prevents a v1 signature from being replayed as a v2
// document (or the reverse).
var mutationRequestDomainV2 = []byte("deepseek-infra:control-mutation-request-v2\x00")

// integerNumberPattern accepts exactly the JSON number literals the Python oracle
// maps to `int`. Anything else (a fraction, an exponent) is a float there and must
// be refused here, or the two implementations would disagree on a signed body.
var integerNumberPattern = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)

// mutationRequestSpec is everything that differs between the frozen v1 document
// and the approved v2 revision. The field set, canonical-JSON rule, checks and
// error codes are shared.
type mutationRequestSpec struct {
	schema             string
	schemaVersion      int64
	signatureDomain    []byte
	allowedOperations  map[string]bool
	intent             string
	payloadFields      []string
	requiresRecordBody bool
}

var mutationRequestFields = []string{
	"actionId", "digest", "domain", "environment", "executionEpoch", "expiresAt",
	"fencingToken", "fleetId", "issuedAt", "mode", "nonce", "operation", "operationId",
	"payload", "payloadDigest", "requestId", "revision", "role", "runtime", "schema",
	"schemaVersion", "signature", "signatureAlgorithm", "signerKeyId",
}

var mutationPayloadFields = []string{"intent", "recordId", "revision", "state"}

// mutationPayloadV2Fields adds the record body the apply operation commits to,
// and nothing else.
var mutationPayloadV2Fields = []string{"intent", "recordId", "recordPayload", "revision", "state"}

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
	// ErrMutationRequestDomainFenced refuses a domain whose mutations belong to
	// the lease and admission path (action/scheduler_run/wave/transfer) rather
	// than to the signed control-mutation channel.
	ErrMutationRequestDomainFenced = errors.New("MUTATION_REQUEST_DOMAIN_FENCED")
)

var allowedMutationOps = map[string]bool{MutationOperationPropose: true}

var allowedMutationV2Ops = map[string]bool{MutationOperationApply: true}

var mutationRequestV1Spec = mutationRequestSpec{
	schema:            MutationRequestSchema,
	schemaVersion:     MutationRequestSchemaVersion,
	signatureDomain:   mutationRequestDomain,
	allowedOperations: allowedMutationOps,
	intent:            MutationIntentShadowCompare,
	payloadFields:     mutationPayloadFields,
}

var mutationRequestV2Spec = mutationRequestSpec{
	schema:             MutationRequestV2Schema,
	schemaVersion:      MutationRequestV2SchemaVersion,
	signatureDomain:    mutationRequestDomainV2,
	allowedOperations:  allowedMutationV2Ops,
	intent:             MutationIntentApply,
	payloadFields:      mutationPayloadV2Fields,
	requiresRecordBody: true,
}

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
	return signMutationRequest(unsigned, privateKey, publicKey, mutationRequestV1Spec)
}

// SignMutationRequestV2 signs a v2 apply-mutation document under the v2 signature
// domain.
func SignMutationRequestV2(unsigned map[string]any, privateKey ed25519.PrivateKey, publicKey string) (map[string]any, []byte, error) {
	return signMutationRequest(unsigned, privateKey, publicKey, mutationRequestV2Spec)
}

func signMutationRequest(unsigned map[string]any, privateKey ed25519.PrivateKey, publicKey string, spec mutationRequestSpec) (map[string]any, []byte, error) {
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
	message := append(append([]byte{}, spec.signatureDomain...), canonical...)
	payload["signature"] = base64.RawURLEncoding.EncodeToString(ed25519.Sign(privateKey, message))
	raw, err := canonicalAuthorityJSON(payload)
	if err != nil {
		return nil, nil, ErrMutationRequestInvalid
	}
	return payload, raw, nil
}

// VerifyMutationRequestDocument verifies a frozen v1 document. Behavior is
// unchanged from the v17 freeze.
func VerifyMutationRequestDocument(raw []byte, context MutationRequestContext) (map[string]any, error) {
	return verifyMutationRequestDocument(raw, context, mutationRequestV1Spec)
}

// VerifyMutationRequestV2Document verifies a v2 apply-mutation document,
// including the record body it commits to.
func VerifyMutationRequestV2Document(raw []byte, context MutationRequestContext) (map[string]any, error) {
	return verifyMutationRequestDocument(raw, context, mutationRequestV2Spec)
}

func verifyMutationRequestDocument(raw []byte, context MutationRequestContext, spec mutationRequestSpec) (map[string]any, error) {
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
	if err := verifyMutationRequestEnvelope(document, context, spec); err != nil {
		return nil, err
	}
	if err := verifyMutationRequestSignature(document, context, spec); err != nil {
		return nil, err
	}
	return document, nil
}

// validateMutationRecordPayload enforces exactly the primitive set the Python
// oracle's canonical encoder accepts: null, string, bool, integer, list, and
// object with string keys. A float is refused on both sides, which is what keeps
// the signed record bytes reproducible in every implementation.
func validateMutationRecordPayload(value any, depth int) error {
	if depth > maxRecordPayloadDepth {
		return ErrMutationRequestInvalid
	}
	switch typed := value.(type) {
	case nil, bool, string:
		return nil
	case json.Number:
		if !integerNumberPattern.MatchString(typed.String()) {
			return ErrMutationRequestInvalid
		}
		return nil
	case []any:
		for _, item := range typed {
			if err := validateMutationRecordPayload(item, depth+1); err != nil {
				return err
			}
		}
		return nil
	case map[string]any:
		for _, item := range typed {
			if err := validateMutationRecordPayload(item, depth+1); err != nil {
				return err
			}
		}
		return nil
	default:
		return ErrMutationRequestInvalid
	}
}

// rejectMutationBodySecretKeys mirrors the Python oracle's mutation-channel key
// rule exactly. It is deliberately stricter than the shared control-record rule
// (`rejectControlSecretMaterial`, which exempts keys ending in
// digest/reference/ref/id/type/provider): Go must never be *looser* than the
// oracle, or a record body the oracle refuses could still be applied here. The
// oracle's flagged set is a subset of the shared rule's, so applying both adds no
// refusal the oracle would accept.
func rejectMutationBodySecretKeys(value any, depth int) error {
	if depth > maxRecordPayloadDepth {
		return ErrMutationRequestInvalid
	}
	switch typed := value.(type) {
	case map[string]any:
		for key, nested := range typed {
			if mutationChannelSecretKey(key) {
				return ErrMutationRequestSecretDetected
			}
			if err := rejectMutationBodySecretKeys(nested, depth+1); err != nil {
				return err
			}
		}
	case []any:
		for _, nested := range typed {
			if err := rejectMutationBodySecretKeys(nested, depth+1); err != nil {
				return err
			}
		}
	}
	return nil
}

func mutationChannelSecretKey(key string) bool {
	var normalized strings.Builder
	for _, character := range strings.ToLower(key) {
		if character >= 'a' && character <= 'z' || character >= '0' && character <= '9' {
			normalized.WriteRune(character)
		}
	}
	name := normalized.String()
	switch name {
	case "fencingtoken", "signature", "signaturealgorithm", "signerkeyid":
		return false
	}
	for _, fragment := range []string{
		"password", "passwd", "privatekey", "ageidentity", "apikey", "accesskey",
		"secretkey", "token", "credential", "oauth", "bearer", "secret",
	} {
		if strings.Contains(name, fragment) {
			return true
		}
	}
	return false
}

func verifyMutationRequestEnvelope(document map[string]any, context MutationRequestContext, spec mutationRequestSpec) error {
	if asString(document["schema"]) != spec.schema || asInt(document["schemaVersion"]) != spec.schemaVersion {
		return ErrMutationRequestSchemaInvalid
	}
	operation := asString(document["operation"])
	if !spec.allowedOperations[operation] || operation != context.ExpectedOperation {
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
	if len(payloadKeys) != len(spec.payloadFields) {
		return ErrMutationRequestInvalid
	}
	for index, key := range payloadKeys {
		if key != spec.payloadFields[index] {
			return ErrMutationRequestInvalid
		}
	}
	if asString(payload["intent"]) != spec.intent {
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
	if spec.requiresRecordBody {
		recordPayload, ok := payload["recordPayload"].(map[string]any)
		if !ok {
			return ErrMutationRequestInvalid
		}
		if err := rejectMutationBodySecretKeys(recordPayload, 0); err != nil {
			return err
		}
		if err := validateMutationRecordPayload(recordPayload, 0); err != nil {
			return err
		}
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

func verifyMutationRequestSignature(document map[string]any, context MutationRequestContext, spec mutationRequestSpec) error {
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
	message := append(append([]byte{}, spec.signatureDomain...), canonical...)
	if !ed25519.Verify(publicKey, message, signature) {
		return ErrMutationRequestSignatureInvalid
	}
	return nil
}

func mutationRequestDigest(document map[string]any) (string, error) {
	return typedDigest(copyWithout(document, "signature", "digest"))
}
