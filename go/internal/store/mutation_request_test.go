package store

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

func loadMutationRequestFixture(t *testing.T) authorityRequestFixture {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve mutation request test source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "compat", "native-runtime", "v17", "control", "mutation_request_vector.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture authorityRequestFixture
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	return fixture
}

func testMutationRequestContext(t *testing.T, fixture authorityRequestFixture, overrides map[string]any) MutationRequestContext {
	t.Helper()
	now, err := time.Parse(time.RFC3339, fixture.Now)
	if err != nil {
		t.Fatal(err)
	}
	context := MutationRequestContext{
		Now:                  now,
		SignerPublicKey:      fixture.SignerPublicKey,
		SignerKeyID:          fixture.SignerKeyID,
		ExpectedDomain:       "policy",
		ExpectedOperation:    MutationOperationPropose,
		ExpectedRuntime:      RuntimeGo,
		ExpectedMode:         ModeShadow,
		ExpectedFleetID:      "fleet-a",
		ExpectedEnvironment:  "test",
		ExpectedRole:         "control-plane",
		CurrentFencingToken:  4,
		LiveEpoch:            3,
		SeenRequestIDs:       map[string]bool{},
		SeenNonces:           map[string]bool{},
		SeenOperationDigests: map[string]string{},
		MaxFutureSkewSeconds: DefaultMutationRequestSkew,
	}
	if overrides == nil {
		return context
	}
	if rawNow, ok := overrides["now"].(string); ok {
		parsed, err := time.Parse(time.RFC3339, rawNow)
		if err != nil {
			t.Fatal(err)
		}
		context.Now = parsed
	}
	if value, ok := overrides["expected_domain"].(string); ok {
		context.ExpectedDomain = value
	}
	if value, ok := overrides["expected_fleet_id"].(string); ok {
		context.ExpectedFleetID = value
	}
	if value, ok := overrides["expected_environment"].(string); ok {
		context.ExpectedEnvironment = value
	}
	if value, ok := overrides["expected_role"].(string); ok {
		context.ExpectedRole = value
	}
	if value, ok := overrides["expected_runtime"].(string); ok {
		context.ExpectedRuntime = value
	}
	if value, ok := overrides["expected_mode"].(string); ok {
		context.ExpectedMode = value
	}
	if value, ok := overrides["signer_key_id"].(string); ok {
		context.SignerKeyID = value
	}
	if value, ok := overrides["current_fencing_token"].(float64); ok {
		context.CurrentFencingToken = int64(value)
	}
	if value, ok := overrides["live_epoch"].(float64); ok {
		context.LiveEpoch = int64(value)
	}
	if values, ok := overrides["seen_request_ids"].([]any); ok {
		for _, value := range values {
			context.SeenRequestIDs[value.(string)] = true
		}
	}
	if values, ok := overrides["seen_nonces"].([]any); ok {
		for _, value := range values {
			context.SeenNonces[value.(string)] = true
		}
	}
	if values, ok := overrides["seen_operation_digests"].(map[string]any); ok {
		for key, value := range values {
			context.SeenOperationDigests[key] = value.(string)
		}
	}
	return context
}

func TestVerifyMutationRequestDocumentAcceptsFrozenVector(t *testing.T) {
	fixture := loadMutationRequestFixture(t)
	document, err := VerifyMutationRequestDocument([]byte(fixture.CanonicalRequest), testMutationRequestContext(t, fixture, nil))
	if err != nil {
		t.Fatal(err)
	}
	if asString(document["digest"]) != fixture.RequestDigest || asString(document["mode"]) != ModeShadow {
		t.Fatalf("verified document: %+v", document)
	}
	if asString(document["operation"]) != MutationOperationPropose || asString(document["domain"]) != "policy" {
		t.Fatalf("mutation identity: %+v", document)
	}
}

func TestVerifyMutationRequestDocumentRejectsFrozenFailures(t *testing.T) {
	fixture := loadMutationRequestFixture(t)
	for _, test := range fixture.Cases {
		t.Run(test.Name, func(t *testing.T) {
			raw := []byte(test.Raw)
			if test.Raw == "" {
				var document map[string]any
				if err := decodeSingleJSON([]byte(fixture.CanonicalRequest), &document); err != nil {
					t.Fatal(err)
				}
				for key, value := range test.Replace {
					if number, ok := value.(float64); ok && number == float64(int64(number)) {
						document[key] = json.Number(strconv.FormatInt(int64(number), 10))
					} else {
						document[key] = value
					}
				}
				if test.RecomputeDigest {
					digest, err := mutationRequestDigest(document)
					if err != nil {
						t.Fatal(err)
					}
					document["digest"] = digest
				}
				if test.Drop != "" {
					delete(document, test.Drop)
				}
				for key, value := range test.Add {
					document[key] = value
				}
				canonical, err := canonicalAuthorityJSON(document)
				if err != nil {
					t.Fatal(err)
				}
				raw = canonical
				if test.Noncanonical {
					indented, err := json.MarshalIndent(document, "", "  ")
					if err != nil {
						t.Fatal(err)
					}
					raw = indented
				}
			}
			_, err := VerifyMutationRequestDocument(raw, testMutationRequestContext(t, fixture, test.Context))
			if err == nil || err.Error() != test.Error {
				t.Fatalf("error=%v want %s", err, test.Error)
			}
		})
	}
}

func TestMutationRequestIdempotentOperationDoesNotApplyProductionMutation(t *testing.T) {
	fixture := loadMutationRequestFixture(t)
	var document map[string]any
	if err := decodeSingleJSON([]byte(fixture.CanonicalRequest), &document); err != nil {
		t.Fatal(err)
	}
	context := testMutationRequestContext(t, fixture, nil)
	context.SeenOperationDigests[asString(document["operationId"])] = asString(document["payloadDigest"])
	if _, err := VerifyMutationRequestDocument([]byte(fixture.CanonicalRequest), context); err != nil {
		t.Fatalf("same operationId and payload must verify: %v", err)
	}
	store := openShadow(t)
	defer store.Close()
	if err := store.MutateProduction("policy", map[string]any{"id": "p1"}); err != internalprotocol.ErrMutationDenied {
		t.Fatalf("verified mutation request must not enable production writes: %v", err)
	}
}

func TestMutationRequestHelpersFailClosed(t *testing.T) {
	if _, err := VerifyMutationRequestDocument(nil, MutationRequestContext{}); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("empty: %v", err)
	}
	if _, _, err := SignMutationRequest(nil, nil, ""); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("nil unsigned: %v", err)
	}
	if _, _, err := SignMutationRequest(map[string]any{"signature": "x"}, nil, ""); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("pre-signed: %v", err)
	}
	if _, _, err := SignMutationRequest(map[string]any{"payload": map[string]any{}}, ed25519.PrivateKey("short"), ""); !errors.Is(err, ErrMutationRequestSignatureInvalid) {
		t.Fatalf("short key: %v", err)
	}
	if _, _, err := SignMutationRequest(map[string]any{"payload": "x"}, make(ed25519.PrivateKey, ed25519.PrivateKeySize), ""); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("payload type: %v", err)
	}
}

func TestSignMutationRequestRoundTripMatchesVerifier(t *testing.T) {
	seed, err := hex.DecodeString("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
	if err != nil {
		t.Fatal(err)
	}
	private := ed25519.NewKeyFromSeed(seed)
	public := base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
	unsigned := map[string]any{
		"schema":         MutationRequestSchema,
		"schemaVersion":  json.Number("1"),
		"domain":         "policy",
		"operation":      MutationOperationPropose,
		"actionId":       "pol-1",
		"executionEpoch": json.Number("4"),
		"fencingToken":   json.Number("4"),
		"revision":       json.Number("1"),
		"requestId":      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"nonce":          "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"operationId":    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		"issuedAt":       "2026-09-05T00:00:30Z",
		"expiresAt":      "2026-09-05T00:05:30Z",
		"runtime":        RuntimeGo,
		"mode":           ModeShadow,
		"fleetId":        "fleet-a",
		"environment":    "test",
		"role":           "control-plane",
		"payload": map[string]any{
			"intent":   MutationIntentShadowCompare,
			"recordId": "p1",
			"revision": json.Number("1"),
			"state":    "ACTIVE",
		},
	}
	if _, _, err := SignMutationRequest(unsigned, private, "not-a-key"); !errors.Is(err, ErrMutationRequestSignerMismatch) {
		t.Fatalf("bad public key: %v", err)
	}
	_, raw, err := SignMutationRequest(unsigned, private, public)
	if err != nil {
		t.Fatal(err)
	}
	fixture := loadMutationRequestFixture(t)
	if _, err := VerifyMutationRequestDocument(raw, testMutationRequestContext(t, fixture, nil)); err != nil {
		t.Fatalf("signed document must verify: %v", err)
	}
}

func TestMutationRequestEnvelopeAndSignatureFailClosed(t *testing.T) {
	fixture := loadMutationRequestFixture(t)
	context := testMutationRequestContext(t, fixture, nil)
	mustFail := func(t *testing.T, mutate func(map[string]any), recompute bool, want error) {
		t.Helper()
		var document map[string]any
		if err := decodeSingleJSON([]byte(fixture.CanonicalRequest), &document); err != nil {
			t.Fatal(err)
		}
		mutate(document)
		if recompute {
			digest, err := mutationRequestDigest(document)
			if err != nil {
				t.Fatal(err)
			}
			document["digest"] = digest
		}
		raw, err := canonicalAuthorityJSON(document)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := VerifyMutationRequestDocument(raw, context); !errors.Is(err, want) {
			t.Fatalf("error=%v want %v", err, want)
		}
	}
	mustFail(t, func(document map[string]any) {
		document["payloadDigest"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
	}, false, ErrMutationRequestPayloadDigestMismatch)
	mustFail(t, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["intent"] = "apply-production"
	}, false, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["state"] = ""
	}, false, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["recordId"] = ".."
	}, false, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["revision"] = json.Number("0")
	}, false, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		document["revision"] = json.Number("0")
	}, false, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		document["issuedAt"] = "2026-09-05T00:00:30"
	}, true, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		document["expiresAt"] = "2026-09-05T00:00:10Z"
	}, true, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		document["expiresAt"] = "2026-09-05T01:00:30Z"
	}, true, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		document["signatureAlgorithm"] = "RSA"
	}, true, ErrMutationRequestSignatureInvalid)
	mustFail(t, func(document map[string]any) {
		document["signature"] = "@@@@"
	}, true, ErrMutationRequestSignatureInvalid)
	mustFail(t, func(document map[string]any) {
		document["payload"] = []any{"x"}
	}, false, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["extra"] = true
	}, false, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		document["fleetId"] = "NOPE"
	}, false, ErrMutationRequestFleetMismatch)
	mustFail(t, func(document map[string]any) {
		document["operationId"] = "not-hex"
	}, false, ErrMutationRequestInvalid)
	mustFail(t, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["password"] = "do-not-store"
	}, false, ErrMutationRequestSecretDetected)
	mustFail(t, func(document map[string]any) {
		document["fencingToken"] = json.Number("0")
	}, false, ErrMutationRequestStaleFencingToken)

	context.MaxFutureSkewSeconds = -1
	var document map[string]any
	if err := decodeSingleJSON([]byte(fixture.CanonicalRequest), &document); err != nil {
		t.Fatal(err)
	}
	document["issuedAt"] = "2026-09-05T00:01:00Z"
	digest, err := mutationRequestDigest(document)
	if err != nil {
		t.Fatal(err)
	}
	document["digest"] = digest
	raw, err := canonicalAuthorityJSON(document)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyMutationRequestDocument(raw, context); !errors.Is(err, ErrMutationRequestFutureSkew) && !errors.Is(err, ErrMutationRequestSignatureInvalid) {
		t.Fatalf("negative skew: %v", err)
	}
	if _, err := VerifyMutationRequestDocument([]byte("{"), MutationRequestContext{}); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("malformed: %v", err)
	}

	var deep any = "leaf"
	for range 130 {
		deep = []any{deep}
	}
	mustFail(t, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["notes"] = deep
	}, false, ErrMutationRequestInvalid)

	validContext := testMutationRequestContext(t, fixture, nil)
	validContext.SignerPublicKey = "YQ"
	if _, err := VerifyMutationRequestDocument([]byte(fixture.CanonicalRequest), validContext); !errors.Is(err, ErrMutationRequestSignatureInvalid) {
		t.Fatalf("short public key: %v", err)
	}
	validContext.SignerPublicKey = base64.RawURLEncoding.EncodeToString(make([]byte, ed25519.PublicKeySize))
	if _, err := VerifyMutationRequestDocument([]byte(fixture.CanonicalRequest), validContext); !errors.Is(err, ErrMutationRequestSignatureInvalid) {
		t.Fatalf("wrong public key: %v", err)
	}
}
