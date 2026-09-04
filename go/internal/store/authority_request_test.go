package store

import (
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

type authorityRequestFixture struct {
	CanonicalRequest string `json:"canonical_request"`
	RequestDigest    string `json:"request_digest"`
	SignerPublicKey  string `json:"signer_public_key"`
	SignerKeyID      string `json:"signer_key_id"`
	Now              string `json:"now"`
	Cases            []authorityRequestCase
}

type authorityRequestCase struct {
	Name            string         `json:"name"`
	Error           string         `json:"error"`
	Raw             string         `json:"raw"`
	Replace         map[string]any `json:"replace"`
	Add             map[string]any `json:"add"`
	Drop            string         `json:"drop"`
	RecomputeDigest bool           `json:"recompute_digest"`
	Noncanonical    bool           `json:"noncanonical"`
	Context         map[string]any `json:"context"`
}

func loadAuthorityRequestFixture(t *testing.T) authorityRequestFixture {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve authority request test source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "compat", "native-runtime", "v7", "control", "authority_request_vector.json")
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

func testAuthorityRequestContext(t *testing.T, fixture authorityRequestFixture, overrides map[string]any) AuthorityRequestContext {
	t.Helper()
	now, err := time.Parse(time.RFC3339, fixture.Now)
	if err != nil {
		t.Fatal(err)
	}
	context := AuthorityRequestContext{
		Now:                  now,
		SignerPublicKey:      fixture.SignerPublicKey,
		SignerKeyID:          fixture.SignerKeyID,
		ExpectedDomain:       "action",
		ExpectedOperation:    "install-epoch",
		ExpectedRuntime:      RuntimeGo,
		ExpectedMode:         ModeShadow,
		ExpectedFleetID:      "fleet-a",
		ExpectedEnvironment:  "test",
		ExpectedRole:         "control-plane",
		CurrentFencingToken:  4,
		LiveEpoch:            3,
		SeenRequestIDs:       map[string]bool{},
		SeenNonces:           map[string]bool{},
		MaxFutureSkewSeconds: DefaultAuthorityRequestSkew,
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
	return context
}

func TestVerifyAuthorityRequestDocumentAcceptsFrozenVector(t *testing.T) {
	fixture := loadAuthorityRequestFixture(t)
	document, err := VerifyAuthorityRequestDocument([]byte(fixture.CanonicalRequest), testAuthorityRequestContext(t, fixture, nil))
	if err != nil {
		t.Fatal(err)
	}
	if asString(document["digest"]) != fixture.RequestDigest || asString(document["mode"]) != ModeShadow {
		t.Fatalf("verified document: %+v", document)
	}
}

func TestVerifyAuthorityRequestDocumentRejectsFrozenFailures(t *testing.T) {
	fixture := loadAuthorityRequestFixture(t)
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
					digest, err := authorityRequestDigest(document)
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
			_, err := VerifyAuthorityRequestDocument(raw, testAuthorityRequestContext(t, fixture, test.Context))
			if err == nil || err.Error() != test.Error {
				t.Fatalf("error=%v want %s", err, test.Error)
			}
		})
	}
}

func TestAuthorityRequestDoesNotEnableProductionMutation(t *testing.T) {
	if !errors.Is(internalprotocol.DenyMutation(), internalprotocol.ErrMutationDenied) {
		t.Fatal("production mutation must stay denied")
	}
}

func TestAuthorityRequestHelpersFailClosed(t *testing.T) {
	if _, err := VerifyAuthorityRequestDocument(nil, AuthorityRequestContext{}); !errors.Is(err, ErrAuthorityRequestInvalid) {
		t.Fatalf("empty: %v", err)
	}
	if asInt(json.Number("4")) != 4 || asInt("x") != 0 || asInt(json.Number("1.5")) != 0 {
		t.Fatal("asInt")
	}
	if _, err := parseAuthorityTimestamp("2026-09-04T00:00:30"); !errors.Is(err, ErrAuthorityRequestInvalid) {
		t.Fatalf("timestamp: %v", err)
	}
	if _, err := parseAuthorityTimestamp("not-a-timeZ"); !errors.Is(err, ErrAuthorityRequestInvalid) {
		t.Fatalf("bad timestamp: %v", err)
	}
	if _, err := decodeFixedBase64("", 32); err == nil {
		t.Fatal("empty b64")
	}
	if _, err := decodeFixedBase64("@@@", 32); err == nil {
		t.Fatal("invalid b64")
	}
	if _, err := decodeFixedBase64("YQ", 32); err == nil {
		t.Fatal("short b64")
	}
	document := map[string]any{"signatureAlgorithm": "RSA", "signerKeyId": "ctrl-signer-0000000000000000"}
	if err := verifyAuthorityRequestSignature(document, AuthorityRequestContext{SignerKeyID: "ctrl-signer-0000000000000000"}); !errors.Is(err, ErrAuthorityRequestSignatureInvalid) {
		t.Fatalf("algorithm: %v", err)
	}
	if _, err := parseAuthorityTimestamp("2026-09-04T00:00:30.1Z"); !errors.Is(err, ErrAuthorityRequestInvalid) {
		t.Fatalf("fractional timestamp: %v", err)
	}
	fixture := loadAuthorityRequestFixture(t)
	var valid map[string]any
	if err := decodeSingleJSON([]byte(fixture.CanonicalRequest), &valid); err != nil {
		t.Fatal(err)
	}
	valid["revision"] = json.Number("0")
	if err := verifyAuthorityRequestEnvelope(valid, testAuthorityRequestContext(t, fixture, nil)); !errors.Is(err, ErrAuthorityRequestInvalid) {
		t.Fatalf("revision: %v", err)
	}
	if err := verifyAuthorityRequestEnvelope(map[string]any{"schema": AuthorityRequestSchema, "schemaVersion": json.Number("1"), "operation": "install-epoch"}, testAuthorityRequestContext(t, fixture, nil)); err == nil {
		t.Fatal("incomplete envelope")
	}
	negativeSkew := testAuthorityRequestContext(t, fixture, nil)
	negativeSkew.MaxFutureSkewSeconds = -1
	if _, err := VerifyAuthorityRequestDocument([]byte(fixture.CanonicalRequest), negativeSkew); err != nil {
		t.Fatalf("negative skew on a past issuedAt must still verify: %v", err)
	}
}
