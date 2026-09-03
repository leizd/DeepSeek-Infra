package store

import (
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func frozenCheckpoint(t *testing.T, index int) *AuthorityCheckpoint {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve authority test source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "compat", "native-runtime", "v1", "control", "authority_checkpoints.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read frozen authority corpus: %v", err)
	}
	var corpus struct {
		SchemaVersion int               `json:"schema_version"`
		Checkpoints   []json.RawMessage `json:"checkpoints"`
	}
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatalf("decode frozen authority corpus: %v", err)
	}
	if corpus.SchemaVersion != 1 || index < 0 || index >= len(corpus.Checkpoints) {
		t.Fatalf("invalid frozen authority corpus index %d", index)
	}
	var checkpoint AuthorityCheckpoint
	if err := json.Unmarshal(corpus.Checkpoints[index], &checkpoint); err != nil {
		t.Fatalf("decode checkpoint fixture: %v", err)
	}
	return &checkpoint
}

func resealCheckpoint(t *testing.T, checkpoint *AuthorityCheckpoint) {
	t.Helper()
	payloadDigest, err := ComputePayloadDigest(checkpoint)
	if err != nil {
		t.Fatalf("compute payload digest: %v", err)
	}
	checkpoint.PayloadDigest = payloadDigest
	digest, err := ComputeCheckpointDigest(checkpoint)
	if err != nil {
		t.Fatalf("compute checkpoint digest: %v", err)
	}
	checkpoint.Digest = digest
}

func TestAuthorityCheckpointMatchesFrozenPythonDigests(t *testing.T) {
	checkpoint := frozenCheckpoint(t, 0)

	payloadDigest, err := ComputePayloadDigest(checkpoint)
	if err != nil {
		t.Fatalf("compute payload digest: %v", err)
	}
	if payloadDigest != checkpoint.PayloadDigest {
		t.Fatalf("payload digest mismatch: got %s want %s", payloadDigest, checkpoint.PayloadDigest)
	}

	digest, err := ComputeCheckpointDigest(checkpoint)
	if err != nil {
		t.Fatalf("compute checkpoint digest: %v", err)
	}
	if digest != checkpoint.Digest {
		t.Fatalf("checkpoint digest mismatch: got %s want %s", digest, checkpoint.Digest)
	}
	if err := VerifyAuthorityCheckpointIntegrity(checkpoint); err != nil {
		t.Fatalf("verify frozen Python checkpoint: %v", err)
	}
}

func TestAuthorityCheckpointValidationFailsClosed(t *testing.T) {
	valid := frozenCheckpoint(t, 0)
	tests := []struct {
		name string
		edit func(*AuthorityCheckpoint)
		want error
	}{
		{name: "schema", edit: func(cp *AuthorityCheckpoint) { cp.Schema = "control-authority-v2" }, want: ErrInvalidAuthoritySchema},
		{name: "generation", edit: func(cp *AuthorityCheckpoint) { cp.AuthorityGeneration = 0 }, want: ErrInvalidAuthorityGeneration},
		{name: "nil policies", edit: func(cp *AuthorityCheckpoint) { cp.Policies = nil }, want: ErrInvalidAuthorityCheckpoint},
		{name: "nil targets", edit: func(cp *AuthorityCheckpoint) { cp.Targets = nil }, want: ErrInvalidAuthorityCheckpoint},
		{name: "nil generation map", edit: func(cp *AuthorityCheckpoint) { cp.PromotionEpochs = nil }, want: ErrInvalidAuthorityCheckpoint},
		{name: "secret", edit: func(cp *AuthorityCheckpoint) { cp.Policies = []any{map[string]any{"secret": "age-secret-key-12345"}} }, want: ErrSecretDetected},
		{name: "pem", edit: func(cp *AuthorityCheckpoint) {
			cp.Targets = []any{map[string]any{"certificate": "-----BEGIN PRIVATE KEY-----"}}
		}, want: ErrSecretDetected},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			checkpoint := *valid
			test.edit(&checkpoint)
			if err := ValidateAuthorityCheckpoint(&checkpoint); !errors.Is(err, test.want) {
				t.Fatalf("validation error = %v, want %v", err, test.want)
			}
		})
	}
	if err := ValidateAuthorityCheckpoint(nil); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
		t.Fatalf("nil validation error = %v, want %v", err, ErrInvalidAuthorityCheckpoint)
	}
	invalidForMarshal := *valid
	invalidForMarshal.Schema = "control-authority-v2"
	if _, err := json.Marshal(invalidForMarshal); !errors.Is(err, ErrInvalidAuthoritySchema) {
		t.Fatalf("invalid checkpoint marshal error = %v", err)
	}
}

func TestAuthorityCheckpointIntegrityRejectsTampering(t *testing.T) {
	tests := []struct {
		name string
		edit func(*AuthorityCheckpoint)
		want error
	}{
		{name: "payload", edit: func(cp *AuthorityCheckpoint) { cp.CreatedAt = "2026-09-03T00:00:02Z" }, want: ErrAuthorityPayloadDigestMismatch},
		{name: "payload digest", edit: func(cp *AuthorityCheckpoint) { cp.PayloadDigest = "0" + cp.PayloadDigest[1:] }, want: ErrAuthorityPayloadDigestMismatch},
		{name: "checkpoint digest", edit: func(cp *AuthorityCheckpoint) { cp.Digest = "0" + cp.Digest[1:] }, want: ErrAuthorityDigestMismatch},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			checkpoint := frozenCheckpoint(t, 0)
			test.edit(checkpoint)
			if err := VerifyAuthorityCheckpointIntegrity(checkpoint); !errors.Is(err, test.want) {
				t.Fatalf("integrity error = %v, want %v", err, test.want)
			}
		})
	}
}

func TestAuthorityChainMatchesFrozenPythonHistory(t *testing.T) {
	genesis := frozenCheckpoint(t, 0)
	second := frozenCheckpoint(t, 1)
	if err := VerifyAuthorityChain([]*AuthorityCheckpoint{second, genesis}); err != nil {
		t.Fatalf("verify out-of-order frozen history: %v", err)
	}
}

func TestAuthorityChainRejectsUnsafeHistories(t *testing.T) {
	genesis := frozenCheckpoint(t, 0)
	second := frozenCheckpoint(t, 1)
	emptyPrevious := ""
	wrongPrevious := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	missingGenesis := *second
	missingGenesis.PreviousDigest = nil
	resealCheckpoint(t, &missingGenesis)

	genesisWithPrevious := *genesis
	genesisWithPrevious.PreviousDigest = &wrongPrevious
	resealCheckpoint(t, &genesisWithPrevious)

	generationGap := *second
	generationGap.AuthorityGeneration = 3
	resealCheckpoint(t, &generationGap)

	brokenChain := *second
	brokenChain.PreviousDigest = &wrongPrevious
	resealCheckpoint(t, &brokenChain)

	fork := *genesis
	fork.CreatedAt = "2026-09-03T00:00:09Z"
	resealCheckpoint(t, &fork)

	genesisWithEmptyPrevious := *genesis
	genesisWithEmptyPrevious.PreviousDigest = &emptyPrevious
	resealCheckpoint(t, &genesisWithEmptyPrevious)

	tests := []struct {
		name    string
		history []*AuthorityCheckpoint
		want    error
	}{
		{name: "empty", history: nil, want: ErrAuthorityEmptyHistory},
		{name: "missing genesis", history: []*AuthorityCheckpoint{&missingGenesis}, want: ErrAuthorityGenerationGap},
		{name: "genesis previous", history: []*AuthorityCheckpoint{&genesisWithPrevious}, want: ErrAuthorityGenesisPreviousDigest},
		{name: "generation gap", history: []*AuthorityCheckpoint{genesis, &generationGap}, want: ErrAuthorityGenerationGap},
		{name: "broken chain", history: []*AuthorityCheckpoint{genesis, &brokenChain}, want: ErrAuthorityBrokenChain},
		{name: "divergent generation", history: []*AuthorityCheckpoint{genesis, &fork}, want: ErrAuthorityFork},
		{name: "empty genesis previous accepted", history: []*AuthorityCheckpoint{&genesisWithEmptyPrevious}, want: nil},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := VerifyAuthorityChain(test.history); !errors.Is(err, test.want) {
				t.Fatalf("chain error = %v, want %v", err, test.want)
			}
		})
	}
}

func TestAuthorityHeadTransitionMatchesFrozenPythonCASRules(t *testing.T) {
	genesis := frozenCheckpoint(t, 0)
	second := frozenCheckpoint(t, 1)

	advance, err := VerifyAuthorityHeadTransition(nil, genesis)
	if err != nil || !advance {
		t.Fatalf("genesis transition = (%v, %v), want advance", advance, err)
	}
	advance, err = VerifyAuthorityHeadTransition(&AuthorityHead{}, genesis)
	if err != nil || !advance {
		t.Fatalf("zero-head genesis transition = (%v, %v), want advance", advance, err)
	}
	advance, err = VerifyAuthorityHeadTransition(
		&AuthorityHead{Generation: genesis.AuthorityGeneration, Digest: genesis.Digest},
		genesis,
	)
	if err != nil || advance {
		t.Fatalf("idempotent transition = (%v, %v), want no-op", advance, err)
	}
	advance, err = VerifyAuthorityHeadTransition(
		&AuthorityHead{Generation: genesis.AuthorityGeneration, Digest: genesis.Digest},
		second,
	)
	if err != nil || !advance {
		t.Fatalf("next transition = (%v, %v), want advance", advance, err)
	}
}

func TestAuthorityHeadTransitionRejectsUnsafeCAS(t *testing.T) {
	genesis := frozenCheckpoint(t, 0)
	second := frozenCheckpoint(t, 1)
	wrongDigest := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

	fork := *genesis
	fork.CreatedAt = "2026-09-03T00:00:09Z"
	resealCheckpoint(t, &fork)

	gap := *second
	gap.AuthorityGeneration = 3
	resealCheckpoint(t, &gap)

	broken := *second
	broken.PreviousDigest = &wrongDigest
	resealCheckpoint(t, &broken)

	tampered := *second
	tampered.CreatedAt = "2026-09-03T00:00:10Z"

	tests := []struct {
		name      string
		current   *AuthorityHead
		candidate *AuthorityCheckpoint
		want      error
	}{
		{name: "invalid current generation", current: &AuthorityHead{Generation: -1, Digest: wrongDigest}, candidate: genesis, want: ErrInvalidAuthorityHead},
		{name: "invalid zero head", current: &AuthorityHead{Digest: wrongDigest}, candidate: genesis, want: ErrInvalidAuthorityHead},
		{name: "invalid current digest", current: &AuthorityHead{Generation: 1, Digest: "not-a-digest"}, candidate: second, want: ErrInvalidAuthorityHead},
		{name: "uppercase current digest", current: &AuthorityHead{Generation: 1, Digest: strings.ToUpper(genesis.Digest)}, candidate: second, want: ErrInvalidAuthorityHead},
		{name: "generation exhausted", current: &AuthorityHead{Generation: 1<<63 - 1, Digest: genesis.Digest}, candidate: second, want: ErrStaleAuthorityWriter},
		{name: "non-genesis first", candidate: second, want: ErrStaleAuthorityWriter},
		{name: "same generation fork", current: &AuthorityHead{Generation: 1, Digest: genesis.Digest}, candidate: &fork, want: ErrAuthorityFork},
		{name: "stale generation", current: &AuthorityHead{Generation: 2, Digest: second.Digest}, candidate: genesis, want: ErrStaleAuthorityWriter},
		{name: "future gap", current: &AuthorityHead{Generation: 1, Digest: genesis.Digest}, candidate: &gap, want: ErrStaleAuthorityWriter},
		{name: "broken previous", current: &AuthorityHead{Generation: 1, Digest: genesis.Digest}, candidate: &broken, want: ErrAuthorityBrokenChain},
		{name: "tampered candidate", current: &AuthorityHead{Generation: 1, Digest: genesis.Digest}, candidate: &tampered, want: ErrAuthorityPayloadDigestMismatch},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			advance, err := VerifyAuthorityHeadTransition(test.current, test.candidate)
			if advance || !errors.Is(err, test.want) {
				t.Fatalf("transition = (%v, %v), want false and %v", advance, err, test.want)
			}
		})
	}
}

func TestAuthorityCheckpointRoundTripPreservesAdditiveFields(t *testing.T) {
	checkpoint := frozenCheckpoint(t, 0)
	checkpoint.AdditionalFields = map[string]any{
		"futureControlField": map[string]any{
			"enabled": true,
			"ratio":   json.Number("1e-6"),
		},
	}
	resealCheckpoint(t, checkpoint)

	raw, err := json.Marshal(checkpoint)
	if err != nil {
		t.Fatalf("marshal checkpoint: %v", err)
	}
	var roundTrip AuthorityCheckpoint
	if err := json.Unmarshal(raw, &roundTrip); err != nil {
		t.Fatalf("unmarshal checkpoint: %v", err)
	}
	if _, found := roundTrip.AdditionalFields["futureControlField"]; !found {
		t.Fatal("additive field was silently discarded")
	}
	if err := VerifyAuthorityCheckpointIntegrity(&roundTrip); err != nil {
		t.Fatalf("verify round-tripped checkpoint: %v", err)
	}

	checkpoint.AdditionalFields["schema"] = "shadow"
	if err := ValidateAuthorityCheckpoint(checkpoint); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
		t.Fatalf("reserved additive validation error = %v", err)
	}
	if _, err := json.Marshal(checkpoint); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
		t.Fatalf("reserved additive field error = %v", err)
	}
}

func TestAuthorityCheckpointJSONDecoderFailsClosed(t *testing.T) {
	for _, raw := range []string{`{"schema":`, `{} {}`, `{} x`, `null`, `[]`} {
		var checkpoint AuthorityCheckpoint
		if err := json.Unmarshal([]byte(raw), &checkpoint); err == nil {
			t.Fatalf("decode %q unexpectedly succeeded", raw)
		}
		if err := checkpoint.UnmarshalJSON([]byte(raw)); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
			t.Fatalf("direct decode %q error = %v", raw, err)
		}
	}

	var nilCheckpoint *AuthorityCheckpoint
	if err := nilCheckpoint.UnmarshalJSON([]byte(`{}`)); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
		t.Fatalf("nil receiver error = %v", err)
	}
}

func TestPythonCanonicalJSONMatchesFrozenScalarSemantics(t *testing.T) {
	value := map[string]any{
		"numbers": []any{
			math.Copysign(0, -1), 0.0, 1.0, 1e-7, 1e-6, 1e-5, 1e-4,
			1e15, 1e16, 1.2345678901234567, 9007199254740991.0,
		},
	}
	raw, err := pythonCanonicalJSON(value)
	if err != nil {
		t.Fatalf("canonical numbers: %v", err)
	}
	want := `{"numbers":[-0.0,0.0,1.0,1e-07,1e-06,1e-05,0.0001,1000000000000000.0,1e+16,1.2345678901234567,9007199254740991.0]}`
	if string(raw) != want {
		t.Fatalf("canonical numbers = %s, want %s", raw, want)
	}

	escaped, err := pythonCanonicalJSON(map[string]any{
		"text": "\"\\\b\f\n\r\t\x00\u2028策略<>&",
	})
	if err != nil {
		t.Fatalf("canonical string: %v", err)
	}
	wantEscaped := "{\"text\":\"\\\"\\\\\\b\\f\\n\\r\\t\\u0000\u2028策略<>&\"}"
	if string(escaped) != wantEscaped {
		t.Fatalf("canonical string = %q, want %q", escaped, wantEscaped)
	}
}

func TestPythonCanonicalJSONSupportsCheckpointJSONTypes(t *testing.T) {
	integer := 7
	var nilInteger *int
	values := []any{
		nil,
		true,
		int8(-8),
		int16(-16),
		int32(-32),
		int64(-64),
		uint(1),
		uint8(8),
		uint16(16),
		uint32(32),
		uint64(64),
		uintptr(128),
		json.Number("123456789012345678901234567890"),
		json.Number("-0"),
		json.Number("1.25e+2"),
		[]any(nil),
		map[string]any(nil),
		[2]string{"a", "b"},
		&integer,
		nilInteger,
		json.RawMessage(`{"raw":1}`),
	}
	for index, value := range values {
		if _, err := pythonCanonicalJSON(value); err != nil {
			t.Fatalf("canonical value %d (%T): %v", index, value, err)
		}
	}
}

func TestPythonCanonicalJSONRejectsNonCheckpointValues(t *testing.T) {
	cycle := map[string]any{}
	cycle["self"] = cycle
	invalidUTF8 := string([]byte{0xff})
	tooDeep := any(nil)
	for range 130 {
		tooDeep = []any{tooDeep}
	}
	tests := []struct {
		name  string
		value any
		want  error
	}{
		{name: "nan", value: math.NaN(), want: ErrInvalidAuthorityCheckpoint},
		{name: "infinity", value: math.Inf(1), want: ErrInvalidAuthorityCheckpoint},
		{name: "invalid number", value: json.Number("01"), want: ErrInvalidAuthorityCheckpoint},
		{name: "overflow number", value: json.Number("1e9999"), want: ErrInvalidAuthorityCheckpoint},
		{name: "bytes", value: []byte("secret"), want: ErrInvalidAuthorityCheckpoint},
		{name: "byte array", value: [1]byte{1}, want: ErrInvalidAuthorityCheckpoint},
		{name: "non-string keys", value: map[int]string{1: "x"}, want: ErrInvalidAuthorityCheckpoint},
		{name: "unsupported", value: make(chan int), want: ErrInvalidAuthorityCheckpoint},
		{name: "invalid utf8", value: invalidUTF8, want: ErrInvalidAuthorityCheckpoint},
		{name: "invalid utf8 key", value: map[string]any{invalidUTF8: true}, want: ErrInvalidAuthorityCheckpoint},
		{name: "bad raw json", value: json.RawMessage(`{"x":`), want: ErrInvalidAuthorityCheckpoint},
		{name: "trailing raw json", value: json.RawMessage(`1 2`), want: ErrInvalidAuthorityCheckpoint},
		{name: "cycle", value: cycle, want: ErrInvalidAuthorityCheckpoint},
		{name: "depth", value: tooDeep, want: ErrInvalidAuthorityCheckpoint},
		{name: "secret key", value: map[string]any{"privateKey": "redacted"}, want: ErrSecretDetected},
		{name: "nested secret key", value: []any{map[string]any{"sessionToken": "redacted"}}, want: ErrSecretDetected},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := pythonCanonicalJSON(test.value); !errors.Is(err, test.want) {
				t.Fatalf("canonical error = %v, want %v", err, test.want)
			}
		})
	}

	if _, err := hashCanonicalJSON(make(chan int)); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
		t.Fatalf("hash error = %v", err)
	}
}

func TestAuthorityCheckpointRejectsSecretBearingKeysButAllowsReferences(t *testing.T) {
	checkpoint := frozenCheckpoint(t, 0)
	checkpoint.Policies = []any{map[string]any{
		"credentialReference":      "vault://prod",
		"credential_provider_type": "vault",
		"credentialProvider":       map[string]any{"type": "vault"},
	}}
	if err := ValidateAuthorityCheckpoint(checkpoint); err != nil {
		t.Fatalf("safe credential references rejected: %v", err)
	}

	checkpoint.Policies = []any{map[string]any{"api-token": "redacted"}}
	if err := ValidateAuthorityCheckpoint(checkpoint); !errors.Is(err, ErrSecretDetected) {
		t.Fatalf("secret key error = %v", err)
	}
	checkpoint.Policies = []any{map[string]any{"certificate": strings.ToUpper("-----begin private key-----")}}
	if err := ValidateAuthorityCheckpoint(checkpoint); !errors.Is(err, ErrSecretDetected) {
		t.Fatalf("secret value error = %v", err)
	}
}

func TestAuthorityHelpersPropagateInvalidCheckpoints(t *testing.T) {
	checkpoint := frozenCheckpoint(t, 0)
	checkpoint.AdditionalFields = map[string]any{"duplicate": make(chan int)}
	for name, call := range map[string]func() error{
		"payload": func() error { _, err := ComputePayloadDigest(checkpoint); return err },
		"digest":  func() error { _, err := ComputeCheckpointDigest(checkpoint); return err },
		"verify":  func() error { return VerifyAuthorityCheckpointIntegrity(checkpoint) },
	} {
		t.Run(name, func(t *testing.T) {
			if err := call(); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
				t.Fatalf("helper error = %v", err)
			}
		})
	}
	if _, err := checkpointDocument(nil); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
		t.Fatalf("nil document error = %v", err)
	}
	valid := frozenCheckpoint(t, 0)
	for _, history := range [][]*AuthorityCheckpoint{{nil, valid}, {valid, nil}, {nil, nil}} {
		if err := VerifyAuthorityChain(history); !errors.Is(err, ErrInvalidAuthorityCheckpoint) {
			t.Fatalf("nil chain member error = %v", err)
		}
	}
}

func FuzzAuthorityCheckpointJSONFailsClosedWithoutPanics(f *testing.F) {
	f.Add([]byte(`{}`))
	f.Add([]byte(`null`))
	f.Add([]byte(`{
		"schema":"control-authority-v1",
		"authorityGeneration":1,
		"previousDigest":null,
		"createdAt":"2026-09-03T00:00:00Z",
		"controlSchemaVersion":8,
		"policies":[],
		"targets":[],
		"receiptMutationGenerations":{},
		"promotionEpochs":{},
		"drainGenerations":{},
		"placementGenerations":{}
	}`))
	f.Fuzz(func(_ *testing.T, raw []byte) {
		var checkpoint AuthorityCheckpoint
		if err := json.Unmarshal(raw, &checkpoint); err != nil {
			return
		}
		_ = ValidateAuthorityCheckpoint(&checkpoint)
		_, _ = ComputePayloadDigest(&checkpoint)
		_, _ = ComputeCheckpointDigest(&checkpoint)
		_ = VerifyAuthorityCheckpointIntegrity(&checkpoint)
		_, _ = json.Marshal(checkpoint)
	})
}
