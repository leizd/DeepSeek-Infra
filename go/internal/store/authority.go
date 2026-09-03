package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

const ControlAuthoritySchema = "control-authority-v1"

var (
	ErrSecretDetected                 = errors.New("SECRET_DETECTED_IN_CHECKPOINT")
	ErrInvalidAuthorityCheckpoint     = errors.New("INVALID_AUTHORITY_CHECKPOINT")
	ErrInvalidAuthoritySchema         = errors.New("INVALID_AUTHORITY_SCHEMA")
	ErrInvalidAuthorityGeneration     = errors.New("INVALID_AUTHORITY_GENERATION")
	ErrAuthorityPayloadDigestMismatch = errors.New("AUTHORITY_PAYLOAD_DIGEST_MISMATCH")
	ErrAuthorityDigestMismatch        = errors.New("AUTHORITY_DIGEST_MISMATCH")
	ErrAuthorityEmptyHistory          = errors.New("AUTHORITY_EMPTY_HISTORY")
	ErrAuthorityGenesisPreviousDigest = errors.New("AUTHORITY_GENESIS_PREVIOUS_DIGEST")
	ErrAuthorityGenerationGap         = errors.New("AUTHORITY_GENERATION_GAP")
	ErrAuthorityBrokenChain           = errors.New("AUTHORITY_BROKEN_CHAIN")
	ErrAuthorityFork                  = errors.New("AUTHORITY_FORK")
)

var (
	jsonNumberType  = reflect.TypeOf(json.Number(""))
	jsonRawType     = reflect.TypeOf(json.RawMessage(nil))
	checkpointNames = map[string]struct{}{
		"schema": {}, "authorityGeneration": {}, "previousDigest": {}, "createdAt": {},
		"controlSchemaVersion": {}, "policies": {}, "targets": {},
		"receiptMutationGenerations": {}, "promotionEpochs": {}, "drainGenerations": {},
		"placementGenerations": {}, "controlBootEpoch": {}, "mutationId": {},
		"payloadDigest": {}, "digest": {},
	}
	allowedSecretAdjacentKeys = map[string]struct{}{
		"credentialreference": {}, "credential_reference": {}, "credentialprovidertype": {},
	}
	forbiddenSecretKeyFragments = []string{
		"secret", "password", "passwd", "token", "apikey", "api_key", "accesskey", "access_key",
		"secretkey", "secret_key", "privatekey", "private_key", "ageidentity", "age_identity",
		"identity", "oauth", "bearer", "session",
	}
)

type AuthorityCheckpoint struct {
	Schema                     string           `json:"schema"`
	AuthorityGeneration        int64            `json:"authorityGeneration"`
	PreviousDigest             *string          `json:"previousDigest"`
	CreatedAt                  string           `json:"createdAt"`
	ControlSchemaVersion       int              `json:"controlSchemaVersion"`
	Policies                   []any            `json:"policies"`
	Targets                    []any            `json:"targets"`
	ReceiptMutationGenerations map[string]int64 `json:"receiptMutationGenerations"`
	PromotionEpochs            map[string]int64 `json:"promotionEpochs"`
	DrainGenerations           map[string]int64 `json:"drainGenerations"`
	PlacementGenerations       map[string]int64 `json:"placementGenerations"`
	ControlBootEpoch           *int64           `json:"controlBootEpoch,omitempty"`
	MutationID                 *string          `json:"mutationId,omitempty"`
	PayloadDigest              string           `json:"payloadDigest,omitempty"`
	Digest                     string           `json:"digest,omitempty"`
	AdditionalFields           map[string]any   `json:"-"`
}

// UnmarshalJSON preserves JSON number identity and additive control-authority-v1
// fields so digest verification does not silently discard payload data.
func (cp *AuthorityCheckpoint) UnmarshalJSON(data []byte) error {
	if cp == nil {
		return ErrInvalidAuthorityCheckpoint
	}
	type checkpointAlias AuthorityCheckpoint
	var decoded checkpointAlias
	if err := decodeSingleJSON(data, &decoded); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidAuthorityCheckpoint, err)
	}

	var document map[string]any
	if err := decodeSingleJSON(data, &document); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidAuthorityCheckpoint, err)
	}
	if document == nil {
		return ErrInvalidAuthorityCheckpoint
	}
	additional := make(map[string]any)
	for name, value := range document {
		if _, known := checkpointNames[name]; known {
			continue
		}
		additional[name] = value
	}
	decoded.AdditionalFields = additional
	*cp = AuthorityCheckpoint(decoded)
	return nil
}

func (cp AuthorityCheckpoint) MarshalJSON() ([]byte, error) {
	if err := ValidateAuthorityCheckpoint(&cp); err != nil {
		return nil, err
	}
	document, err := checkpointDocument(&cp)
	if err != nil {
		return nil, err
	}
	return pythonCanonicalJSON(document)
}

func ValidateAuthorityCheckpoint(cp *AuthorityCheckpoint) error {
	if cp == nil {
		return ErrInvalidAuthorityCheckpoint
	}
	if cp.Schema != ControlAuthoritySchema {
		return ErrInvalidAuthoritySchema
	}
	if cp.AuthorityGeneration < 1 {
		return ErrInvalidAuthorityGeneration
	}
	if cp.Policies == nil || cp.Targets == nil || cp.ReceiptMutationGenerations == nil ||
		cp.PromotionEpochs == nil || cp.DrainGenerations == nil || cp.PlacementGenerations == nil {
		return ErrInvalidAuthorityCheckpoint
	}
	document, err := checkpointDocument(cp)
	if err != nil {
		return err
	}
	raw, err := pythonCanonicalJSON(document)
	if err != nil {
		return err
	}
	lower := strings.ToLower(string(raw))
	if strings.Contains(lower, "age-secret-key-") || strings.Contains(lower, "-----begin") {
		return ErrSecretDetected
	}
	return nil
}

// ComputePayloadDigest reproduces the frozen Python control-authority-v1
// payload hash. previousDigest, payloadDigest, and digest are envelope fields.
func ComputePayloadDigest(cp *AuthorityCheckpoint) (string, error) {
	if err := ValidateAuthorityCheckpoint(cp); err != nil {
		return "", err
	}
	document, err := checkpointDocument(cp)
	if err != nil {
		return "", err
	}
	delete(document, "previousDigest")
	delete(document, "payloadDigest")
	delete(document, "digest")
	return hashCanonicalJSON(document)
}

// ComputeCheckpointDigest reproduces the frozen Python four-field envelope
// hash and deliberately excludes all payload fields except payloadDigest.
func ComputeCheckpointDigest(cp *AuthorityCheckpoint) (string, error) {
	if err := ValidateAuthorityCheckpoint(cp); err != nil {
		return "", err
	}
	var previous any
	if cp.PreviousDigest != nil {
		previous = *cp.PreviousDigest
	}
	envelope := map[string]any{
		"authorityGeneration": cp.AuthorityGeneration,
		"previousDigest":      previous,
		"payloadDigest":       cp.PayloadDigest,
		"schema":              cp.Schema,
	}
	return hashCanonicalJSON(envelope)
}

func VerifyAuthorityCheckpointIntegrity(cp *AuthorityCheckpoint) error {
	if err := ValidateAuthorityCheckpoint(cp); err != nil {
		return err
	}
	payloadDigest, err := ComputePayloadDigest(cp)
	if err != nil {
		return err
	}
	if cp.PayloadDigest != payloadDigest {
		return fmt.Errorf("%w: generation %d", ErrAuthorityPayloadDigestMismatch, cp.AuthorityGeneration)
	}
	digest, err := ComputeCheckpointDigest(cp)
	if err != nil {
		return err
	}
	if cp.Digest != digest {
		return fmt.Errorf("%w: generation %d", ErrAuthorityDigestMismatch, cp.AuthorityGeneration)
	}
	return nil
}

// VerifyAuthorityChain accepts any input order but requires one contiguous,
// fork-free genesis-to-head history with byte-compatible Python v1 digests.
func VerifyAuthorityChain(checkpoints []*AuthorityCheckpoint) error {
	if len(checkpoints) == 0 {
		return ErrAuthorityEmptyHistory
	}
	ordered := append([]*AuthorityCheckpoint(nil), checkpoints...)
	sort.SliceStable(ordered, func(left, right int) bool {
		if ordered[left] == nil {
			return ordered[right] != nil
		}
		if ordered[right] == nil {
			return false
		}
		return ordered[left].AuthorityGeneration < ordered[right].AuthorityGeneration
	})

	seen := make(map[int64]string, len(ordered))
	var previousDigest string
	var previousGeneration int64
	for _, checkpoint := range ordered {
		if err := VerifyAuthorityCheckpointIntegrity(checkpoint); err != nil {
			return err
		}
		generation := checkpoint.AuthorityGeneration
		if digest, exists := seen[generation]; exists && digest != checkpoint.Digest {
			return fmt.Errorf("%w: generation %d", ErrAuthorityFork, generation)
		}
		if previousGeneration == 0 {
			if checkpoint.PreviousDigest != nil && *checkpoint.PreviousDigest != "" {
				return ErrAuthorityGenesisPreviousDigest
			}
			if generation != 1 {
				return fmt.Errorf("%w: expected genesis 1, got %d", ErrAuthorityGenerationGap, generation)
			}
		} else {
			if generation != previousGeneration+1 {
				return fmt.Errorf("%w: expected %d, got %d", ErrAuthorityGenerationGap, previousGeneration+1, generation)
			}
			if checkpoint.PreviousDigest == nil || *checkpoint.PreviousDigest != previousDigest {
				return fmt.Errorf("%w: generation %d", ErrAuthorityBrokenChain, generation)
			}
		}
		seen[generation] = checkpoint.Digest
		previousDigest = checkpoint.Digest
		previousGeneration = generation
	}
	return nil
}

func checkpointDocument(cp *AuthorityCheckpoint) (map[string]any, error) {
	if cp == nil {
		return nil, ErrInvalidAuthorityCheckpoint
	}
	var previous any
	if cp.PreviousDigest != nil {
		previous = *cp.PreviousDigest
	}
	document := map[string]any{
		"schema":                     cp.Schema,
		"authorityGeneration":        cp.AuthorityGeneration,
		"previousDigest":             previous,
		"createdAt":                  cp.CreatedAt,
		"controlSchemaVersion":       cp.ControlSchemaVersion,
		"policies":                   cp.Policies,
		"targets":                    cp.Targets,
		"receiptMutationGenerations": cp.ReceiptMutationGenerations,
		"promotionEpochs":            cp.PromotionEpochs,
		"drainGenerations":           cp.DrainGenerations,
		"placementGenerations":       cp.PlacementGenerations,
	}
	if cp.ControlBootEpoch != nil {
		document["controlBootEpoch"] = *cp.ControlBootEpoch
	}
	if cp.MutationID != nil {
		document["mutationId"] = *cp.MutationID
	}
	if cp.PayloadDigest != "" {
		document["payloadDigest"] = cp.PayloadDigest
	}
	if cp.Digest != "" {
		document["digest"] = cp.Digest
	}
	for name, value := range cp.AdditionalFields {
		if _, reserved := checkpointNames[name]; reserved {
			return nil, fmt.Errorf("%w: duplicate field %s", ErrInvalidAuthorityCheckpoint, name)
		}
		document[name] = value
	}
	return document, nil
}

func hashCanonicalJSON(value any) (string, error) {
	raw, err := pythonCanonicalJSON(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:]), nil
}

type canonicalVisit struct {
	typeName reflect.Type
	pointer  uintptr
}

type canonicalEncoder struct {
	buffer bytes.Buffer
	active map[canonicalVisit]struct{}
}

func pythonCanonicalJSON(value any) ([]byte, error) {
	encoder := canonicalEncoder{active: make(map[canonicalVisit]struct{})}
	if err := encoder.append(reflect.ValueOf(value), 0); err != nil {
		return nil, err
	}
	return encoder.buffer.Bytes(), nil
}

func (encoder *canonicalEncoder) append(value reflect.Value, depth int) error {
	if depth > 128 {
		return fmt.Errorf("%w: nesting exceeds 128", ErrInvalidAuthorityCheckpoint)
	}
	if !value.IsValid() {
		encoder.buffer.WriteString("null")
		return nil
	}
	if value.Kind() == reflect.Interface {
		if value.IsNil() {
			encoder.buffer.WriteString("null")
			return nil
		}
		return encoder.append(value.Elem(), depth)
	}
	if value.Type() == jsonNumberType {
		return encoder.appendNumber(value.Interface().(json.Number))
	}
	if value.Type() == jsonRawType {
		return encoder.appendRawJSON(value.Interface().(json.RawMessage), depth)
	}
	if value.Kind() == reflect.Pointer {
		if value.IsNil() {
			encoder.buffer.WriteString("null")
			return nil
		}
		return encoder.withVisit(value, func() error { return encoder.append(value.Elem(), depth) })
	}

	switch value.Kind() {
	case reflect.Bool:
		encoder.buffer.WriteString(strconv.FormatBool(value.Bool()))
	case reflect.String:
		return encoder.appendString(value.String())
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		encoder.buffer.WriteString(strconv.FormatInt(value.Int(), 10))
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		encoder.buffer.WriteString(strconv.FormatUint(value.Uint(), 10))
	case reflect.Float32, reflect.Float64:
		rendered, err := pythonFloat(value.Float())
		if err != nil {
			return err
		}
		encoder.buffer.WriteString(rendered)
	case reflect.Slice, reflect.Array:
		if value.Kind() == reflect.Slice && value.IsNil() {
			encoder.buffer.WriteString("null")
			return nil
		}
		if value.Type().Elem().Kind() == reflect.Uint8 {
			return fmt.Errorf("%w: byte strings are not JSON values", ErrInvalidAuthorityCheckpoint)
		}
		return encoder.withVisit(value, func() error { return encoder.appendArray(value, depth) })
	case reflect.Map:
		if value.IsNil() {
			encoder.buffer.WriteString("null")
			return nil
		}
		if value.Type().Key().Kind() != reflect.String {
			return fmt.Errorf("%w: object keys must be strings", ErrInvalidAuthorityCheckpoint)
		}
		return encoder.withVisit(value, func() error { return encoder.appendMap(value, depth) })
	default:
		return fmt.Errorf("%w: unsupported JSON type %s", ErrInvalidAuthorityCheckpoint, value.Type())
	}
	return nil
}

func (encoder *canonicalEncoder) appendArray(value reflect.Value, depth int) error {
	encoder.buffer.WriteByte('[')
	for index := 0; index < value.Len(); index++ {
		if index > 0 {
			encoder.buffer.WriteByte(',')
		}
		if err := encoder.append(value.Index(index), depth+1); err != nil {
			return err
		}
	}
	encoder.buffer.WriteByte(']')
	return nil
}

func (encoder *canonicalEncoder) appendMap(value reflect.Value, depth int) error {
	keys := value.MapKeys()
	sort.Slice(keys, func(left, right int) bool { return keys[left].String() < keys[right].String() })
	encoder.buffer.WriteByte('{')
	for index, key := range keys {
		if index > 0 {
			encoder.buffer.WriteByte(',')
		}
		if secretBearingCheckpointKey(key.String()) {
			return ErrSecretDetected
		}
		if err := encoder.appendString(key.String()); err != nil {
			return err
		}
		encoder.buffer.WriteByte(':')
		if err := encoder.append(value.MapIndex(key), depth+1); err != nil {
			return err
		}
	}
	encoder.buffer.WriteByte('}')
	return nil
}

func (encoder *canonicalEncoder) appendString(value string) error {
	if !utf8.ValidString(value) {
		return fmt.Errorf("%w: invalid UTF-8", ErrInvalidAuthorityCheckpoint)
	}
	encoder.buffer.WriteByte('"')
	for _, character := range value {
		switch character {
		case '"', '\\':
			encoder.buffer.WriteByte('\\')
			encoder.buffer.WriteRune(character)
		case '\b':
			encoder.buffer.WriteString("\\b")
		case '\f':
			encoder.buffer.WriteString("\\f")
		case '\n':
			encoder.buffer.WriteString("\\n")
		case '\r':
			encoder.buffer.WriteString("\\r")
		case '\t':
			encoder.buffer.WriteString("\\t")
		default:
			if character < 0x20 {
				fmt.Fprintf(&encoder.buffer, "\\u%04x", character)
			} else {
				encoder.buffer.WriteRune(character)
			}
		}
	}
	encoder.buffer.WriteByte('"')
	return nil
}

func (encoder *canonicalEncoder) appendNumber(number json.Number) error {
	raw := string(number)
	if !json.Valid([]byte(raw)) || raw == "true" || raw == "false" || raw == "null" {
		return fmt.Errorf("%w: invalid JSON number", ErrInvalidAuthorityCheckpoint)
	}
	if !strings.ContainsAny(raw, ".eE") {
		if raw == "-0" {
			raw = "0"
		}
		encoder.buffer.WriteString(raw)
		return nil
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return fmt.Errorf("%w: invalid JSON float", ErrInvalidAuthorityCheckpoint)
	}
	rendered, _ := pythonFloat(value)
	encoder.buffer.WriteString(rendered)
	return nil
}

func (encoder *canonicalEncoder) appendRawJSON(raw json.RawMessage, depth int) error {
	var value any
	if err := decodeSingleJSON(raw, &value); err != nil {
		return fmt.Errorf("%w: invalid raw JSON", ErrInvalidAuthorityCheckpoint)
	}
	return encoder.append(reflect.ValueOf(value), depth)
}

func decodeSingleJSON(raw []byte, destination any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("multiple JSON values")
		}
		return err
	}
	return nil
}

func secretBearingCheckpointKey(key string) bool {
	var normalized strings.Builder
	for _, character := range strings.ToLower(key) {
		if character >= 'a' && character <= 'z' || character >= '0' && character <= '9' || character == '_' {
			normalized.WriteRune(character)
		}
	}
	value := normalized.String()
	if _, allowed := allowedSecretAdjacentKeys[value]; allowed || value == "credentialprovider" {
		return false
	}
	for _, fragment := range forbiddenSecretKeyFragments {
		if strings.Contains(value, fragment) {
			return true
		}
	}
	return false
}

func (encoder *canonicalEncoder) withVisit(value reflect.Value, appendValue func() error) error {
	if value.Kind() == reflect.Array {
		return appendValue()
	}
	pointer := value.Pointer()
	visit := canonicalVisit{typeName: value.Type(), pointer: pointer}
	if _, found := encoder.active[visit]; found {
		return fmt.Errorf("%w: cyclic JSON value", ErrInvalidAuthorityCheckpoint)
	}
	encoder.active[visit] = struct{}{}
	defer delete(encoder.active, visit)
	return appendValue()
}

func pythonFloat(value float64) (string, error) {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return "", fmt.Errorf("%w: non-finite number", ErrInvalidAuthorityCheckpoint)
	}
	if value == 0 {
		if math.Signbit(value) {
			return "-0.0", nil
		}
		return "0.0", nil
	}
	scientific := strconv.FormatFloat(value, 'e', -1, 64)
	exponentAt := strings.LastIndexByte(scientific, 'e')
	exponent, _ := strconv.Atoi(scientific[exponentAt+1:])
	if exponent >= -4 && exponent < 16 {
		fixed := strconv.FormatFloat(value, 'f', -1, 64)
		if !strings.ContainsRune(fixed, '.') {
			fixed += ".0"
		}
		return fixed, nil
	}
	return scientific, nil
}
