package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

// mutationV2Fixture is a dedicated shape so the shared v1 fixture struct stays
// untouched. It is decoded with UseNumber so a case's numeric replacements behave
// like the frozen document's numbers instead of collapsing to float64.
type mutationV2Fixture struct {
	CanonicalRequest     string                 `json:"canonical_request"`
	RequestDigest        string                 `json:"request_digest"`
	SignerPublicKey      string                 `json:"signer_public_key"`
	SignerKeyID          string                 `json:"signer_key_id"`
	Now                  string                 `json:"now"`
	MaxFutureSkewSeconds int64                  `json:"max_future_skew_seconds"`
	Context              map[string]any         `json:"context"`
	Cases                []authorityRequestCase `json:"cases"`
}

func loadMutationV2Fixture(t *testing.T) mutationV2Fixture {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve mutation v2 test source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "compat", "native-runtime", "v32", "control", "mutation_request_v2_vector.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read frozen v2 corpus: %v", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var fixture mutationV2Fixture
	if err := decoder.Decode(&fixture); err != nil {
		t.Fatalf("decode frozen v2 corpus: %v", err)
	}
	return fixture
}

func mutationV2Context(t *testing.T, fixture mutationV2Fixture, overrides map[string]any) MutationRequestContext {
	t.Helper()
	now, err := time.Parse(time.RFC3339, fixture.Now)
	if err != nil {
		t.Fatal(err)
	}
	frozen := fixture.Context
	context := MutationRequestContext{
		Now:                  now,
		SignerPublicKey:      fixture.SignerPublicKey,
		SignerKeyID:          fixture.SignerKeyID,
		ExpectedDomain:       asString(frozen["expected_domain"]),
		ExpectedOperation:    asString(frozen["expected_operation"]),
		ExpectedRuntime:      asString(frozen["expected_runtime"]),
		ExpectedMode:         asString(frozen["expected_mode"]),
		ExpectedFleetID:      asString(frozen["expected_fleet_id"]),
		ExpectedEnvironment:  asString(frozen["expected_environment"]),
		ExpectedRole:         asString(frozen["expected_role"]),
		CurrentFencingToken:  asInt(frozen["current_fencing_token"]),
		LiveEpoch:            asInt(frozen["live_epoch"]),
		SeenRequestIDs:       map[string]bool{},
		SeenNonces:           map[string]bool{},
		SeenOperationDigests: map[string]string{},
		MaxFutureSkewSeconds: fixture.MaxFutureSkewSeconds,
	}
	for key, value := range overrides {
		switch key {
		case "now":
			parsed, err := time.Parse(time.RFC3339, asString(value))
			if err != nil {
				t.Fatal(err)
			}
			context.Now = parsed
		case "expected_domain":
			context.ExpectedDomain = asString(value)
		case "expected_operation":
			context.ExpectedOperation = asString(value)
		case "expected_runtime":
			context.ExpectedRuntime = asString(value)
		case "expected_mode":
			context.ExpectedMode = asString(value)
		case "expected_fleet_id":
			context.ExpectedFleetID = asString(value)
		case "expected_environment":
			context.ExpectedEnvironment = asString(value)
		case "expected_role":
			context.ExpectedRole = asString(value)
		case "signer_key_id":
			context.SignerKeyID = asString(value)
		case "current_fencing_token":
			context.CurrentFencingToken = asInt(value)
		case "live_epoch":
			context.LiveEpoch = asInt(value)
		case "seen_request_ids":
			for _, item := range value.([]any) {
				context.SeenRequestIDs[asString(item)] = true
			}
		case "seen_nonces":
			for _, item := range value.([]any) {
				context.SeenNonces[asString(item)] = true
			}
		case "seen_operation_digests":
			for digestKey, digest := range value.(map[string]any) {
				context.SeenOperationDigests[digestKey] = asString(digest)
			}
		default:
			t.Fatalf("unhandled v2 case context override %q", key)
		}
	}
	return context
}

func mutationV2Error(code string) error {
	switch code {
	case "MUTATION_REQUEST_INVALID":
		return ErrMutationRequestInvalid
	case "MUTATION_REQUEST_TOO_LARGE":
		return ErrMutationRequestTooLarge
	case "MUTATION_REQUEST_SCHEMA_INVALID":
		return ErrMutationRequestSchemaInvalid
	case "MUTATION_REQUEST_FIELDS_INVALID":
		return ErrMutationRequestFieldsInvalid
	case "MUTATION_REQUEST_CANONICAL_MISMATCH":
		return ErrMutationRequestCanonicalMismatch
	case "MUTATION_REQUEST_DIGEST_MISMATCH":
		return ErrMutationRequestDigestMismatch
	case "MUTATION_REQUEST_PAYLOAD_DIGEST_MISMATCH":
		return ErrMutationRequestPayloadDigestMismatch
	case "MUTATION_REQUEST_SIGNATURE_INVALID":
		return ErrMutationRequestSignatureInvalid
	case "MUTATION_REQUEST_EXPIRED":
		return ErrMutationRequestExpired
	case "MUTATION_REQUEST_FUTURE_SKEW":
		return ErrMutationRequestFutureSkew
	case "MUTATION_REQUEST_REPLAY":
		return ErrMutationRequestReplay
	case "MUTATION_REQUEST_NONCE_REUSE":
		return ErrMutationRequestNonceReuse
	case "MUTATION_REQUEST_REPLAY_CONFLICT":
		return ErrMutationRequestReplayConflict
	case "MUTATION_REQUEST_DOMAIN_MISMATCH":
		return ErrMutationRequestDomainMismatch
	case "MUTATION_REQUEST_FLEET_MISMATCH":
		return ErrMutationRequestFleetMismatch
	case "MUTATION_REQUEST_ENVIRONMENT_MISMATCH":
		return ErrMutationRequestEnvironmentMismatch
	case "MUTATION_REQUEST_ROLE_MISMATCH":
		return ErrMutationRequestRoleMismatch
	case "MUTATION_REQUEST_RUNTIME_MISMATCH":
		return ErrMutationRequestRuntimeMismatch
	case "MUTATION_REQUEST_MODE_MISMATCH":
		return ErrMutationRequestModeMismatch
	case "MUTATION_REQUEST_OPERATION_INVALID":
		return ErrMutationRequestOperationInvalid
	case "MUTATION_REQUEST_STALE_FENCING_TOKEN":
		return ErrMutationRequestStaleFencingToken
	case "MUTATION_REQUEST_SIGNER_MISMATCH":
		return ErrMutationRequestSignerMismatch
	case "MUTATION_REQUEST_SECRET_DETECTED":
		return ErrMutationRequestSecretDetected
	case "EMPTY_ACTION_ID":
		return internalprotocol.ErrEmptyActionID
	case "ZERO_EXECUTION_EPOCH":
		return internalprotocol.ErrZeroEpoch
	case "STALE_EXECUTION_EPOCH":
		return internalprotocol.ErrStaleEpoch
	default:
		panic("unmapped frozen v2 error code: " + code)
	}
}

func TestMutationRequestV2MatchesFrozenCorpus(t *testing.T) {
	fixture := loadMutationV2Fixture(t)
	verified, err := VerifyMutationRequestV2Document([]byte(fixture.CanonicalRequest), mutationV2Context(t, fixture, nil))
	if err != nil {
		t.Fatalf("frozen v2 request must verify: %v", err)
	}
	if asString(verified["digest"]) != fixture.RequestDigest {
		t.Fatalf("digest=%s want %s", asString(verified["digest"]), fixture.RequestDigest)
	}
	if asString(verified["schema"]) != MutationRequestV2Schema || asInt(verified["schemaVersion"]) != MutationRequestV2SchemaVersion {
		t.Fatalf("schema identity: %v/%v", verified["schema"], verified["schemaVersion"])
	}
	if asString(verified["operation"]) != MutationOperationApply {
		t.Fatalf("operation: %v", verified["operation"])
	}
	payload, ok := verified["payload"].(map[string]any)
	if !ok {
		t.Fatal("payload shape")
	}
	body, ok := payload["recordPayload"].(map[string]any)
	if !ok || asString(body["name"]) != "approved policy" || asInt(body["priority"]) != 2 {
		t.Fatalf("record body: %+v", payload["recordPayload"])
	}
	if len(fixture.Cases) != 34 {
		t.Fatalf("frozen case count = %d, want 34", len(fixture.Cases))
	}
}

func TestMutationRequestV2FailClosedCaseCorpus(t *testing.T) {
	fixture := loadMutationV2Fixture(t)
	for _, test := range fixture.Cases {
		t.Run(test.Name, func(t *testing.T) {
			var document map[string]any
			if err := decodeSingleJSON([]byte(fixture.CanonicalRequest), &document); err != nil {
				t.Fatal(err)
			}
			var raw []byte
			if test.Raw != "" {
				raw = []byte(test.Raw)
			} else {
				for key, value := range test.Replace {
					document[key] = value
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
					raw, err = json.MarshalIndent(document, "", "  ")
					if err != nil {
						t.Fatal(err)
					}
				}
			}
			_, err := VerifyMutationRequestV2Document(raw, mutationV2Context(t, fixture, test.Context))
			if !errors.Is(err, mutationV2Error(test.Error)) {
				t.Fatalf("error=%v want %s", err, test.Error)
			}
		})
	}
}

// The two revisions must not share a signature domain: a v2 document signed under
// the v1 domain is refused, and the v1 verifier refuses a v2 document.
func TestMutationRequestRevisionsAreSignatureDisjoint(t *testing.T) {
	fixture := loadMutationV2Fixture(t)
	var document map[string]any
	if err := decodeSingleJSON([]byte(fixture.CanonicalRequest), &document); err != nil {
		t.Fatal(err)
	}
	unsigned := copyWithout(document, "signature")
	canonical, err := canonicalAuthorityJSON(unsigned)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(mutationRequestDomain, mutationRequestDomainV2) {
		t.Fatal("v1 and v2 must not share a signature domain")
	}
	message := append(append([]byte{}, mutationRequestDomain...), canonical...)
	private, public := rfc8032MutationKeys(t)
	if public != fixture.SignerPublicKey {
		t.Fatalf("fixture key mismatch: %s vs %s", public, fixture.SignerPublicKey)
	}
	resigned := copyWithout(document)
	resigned["signature"] = base64.RawURLEncoding.EncodeToString(ed25519.Sign(private, message))
	raw, err := canonicalAuthorityJSON(resigned)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyMutationRequestV2Document(raw, mutationV2Context(t, fixture, nil)); !errors.Is(err, ErrMutationRequestSignatureInvalid) {
		t.Fatalf("v1-domain signature accepted as v2: %v", err)
	}
	if _, err := VerifyMutationRequestDocument([]byte(fixture.CanonicalRequest), mutationV2Context(t, fixture, nil)); !errors.Is(err, ErrMutationRequestSchemaInvalid) {
		t.Fatalf("v2 document accepted by the v1 verifier: %v", err)
	}
}

// --- apply-mutation end to end -----------------------------------------------

// promoteForApply drives the real authorized cutover so the domain is durably
// Go-authoritative, then reports the cutover record a request must be bound to.
func promoteForApply(t *testing.T, control *Control, domain string) CutoverRecord {
	t.Helper()
	if domain != "policy" && domain != "target" {
		authority := frozenCheckpoint(t, 0)
		if _, advanced, err := control.ClaimControlAuthority(authority); err != nil || !advanced {
			t.Fatalf("claim: advanced=%v %v", advanced, err)
		}
		current := dualEvaluate(t, control, domain)
		promoted, err := signedTransition(t, control, CutoverTransition{
			Domain: domain, To: CutoverGoAuthoritative,
			ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
			TransferID: domain + "-authoritative", Authority: authority,
		})
		if err != nil {
			t.Fatal(err)
		}
		return promoted
	}
	authority, current, result := importEmptyPythonSource(t, control, domain)
	promoted, err := signedTransition(t, control, CutoverTransition{
		Domain: domain, To: CutoverGoAuthoritative,
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
		TransferID: result.TransferID, Authority: authority,
	})
	if err != nil {
		t.Fatal(err)
	}
	return promoted
}

// signedApply builds a v2 apply-mutation document bound to a cutover record. The
// request and nonce identity are derived from the operation id so each operation
// is unique without hand-threading three hex strings through every case.
func signedApply(t *testing.T, cutover CutoverRecord, public string, operationID, recordID, state string, recordRevision int64, body map[string]any) []byte {
	t.Helper()
	private, _ := rfc8032PrivateKeys(t, public)
	if cutover.Domain == "policy" || cutover.Domain == "target" {
		copied := make(map[string]any, len(body)+2)
		for key, value := range body {
			copied[key] = value
		}
		if cutover.Domain == "policy" {
			copied["policyId"] = recordID
			copied["policyRevision"] = json.Number(fmt.Sprint(recordRevision))
		} else {
			copied["targetId"] = recordID
			copied["topologyGeneration"] = json.Number(fmt.Sprint(recordRevision))
		}
		body = copied
	}
	unsigned := map[string]any{
		"schema":         MutationRequestV2Schema,
		"schemaVersion":  json.Number(fmt.Sprint(MutationRequestV2SchemaVersion)),
		"operation":      MutationOperationApply,
		"domain":         cutover.Domain,
		"runtime":        RuntimeGo,
		"mode":           ModeShadow,
		"role":           "control-plane",
		"fleetId":        "fleet-a",
		"environment":    "test",
		"actionId":       "apply-1",
		"executionEpoch": json.Number(fmt.Sprint(cutover.Epoch + 1)),
		"revision":       json.Number(fmt.Sprint(cutover.Revision)),
		"fencingToken":   json.Number(fmt.Sprint(cutover.FencingToken)),
		"requestId":      derivedHex("request:" + operationID),
		"nonce":          derivedHex("nonce:" + operationID),
		"operationId":    operationID,
		"issuedAt":       "2026-09-05T00:00:30Z",
		"expiresAt":      "2026-09-05T00:05:30Z",
		"payload": map[string]any{
			"intent":        MutationIntentApply,
			"recordId":      recordID,
			"recordPayload": body,
			"revision":      json.Number(fmt.Sprint(recordRevision)),
			"state":         state,
		},
	}
	_, raw, err := SignMutationRequestV2(unsigned, private, public)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func derivedHex(seed string) string {
	sum := sha256.Sum256([]byte(seed))
	return hex.EncodeToString(sum[:])
}

func repeatHex(character string) string {
	out := ""
	for index := 0; index < 64; index++ {
		out += character
	}
	return out
}

func rfc8032PrivateKeys(t *testing.T, public string) ([]byte, string) {
	t.Helper()
	private, derived := rfc8032MutationKeys(t)
	if derived != public {
		t.Fatalf("public key mismatch: %s vs %s", derived, public)
	}
	return private, derived
}

func applyAuthority(public string) MutationAuthority {
	return MutationAuthority{
		SignerPublicKey: public,
		FleetID:         "fleet-a",
		Environment:     "test",
		Now:             time.Date(2026, 9, 5, 0, 0, 40, 0, time.UTC),
	}
}

func TestApplyMutationAppliesOnceAndIsIdempotent(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	cutover := promoteForApply(t, control, "policy")
	_, public := rfc8032MutationKeys(t)
	raw := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{
		"enabled":  true,
		"name":     "approved policy",
		"priority": json.Number("2"),
		"tags":     []any{"a", "b"},
	})

	result, err := control.ApplyMutation(raw, applyAuthority(public))
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if result.Status != MutationApplied || result.OperationID != repeatHex("c") || result.Domain != "policy" {
		t.Fatalf("apply result: %+v", result)
	}
	record, exists, err := control.Get("policy", "p1")
	if err != nil || !exists {
		t.Fatalf("applied record: exists=%v %v", exists, err)
	}
	if record.State != "ACTIVE" || record.Revision != 1 {
		t.Fatalf("applied record shape: %+v", record)
	}
	var body map[string]any
	if err := decodeSingleJSON(record.Payload, &body); err != nil {
		t.Fatal(err)
	}
	if body["name"] != "approved policy" || body["enabled"] != true {
		t.Fatalf("applied body: %+v", body)
	}
	stored, ok, err := control.GetOperation(repeatHex("c"))
	if err != nil || !ok || stored.Status != MutationApplied {
		t.Fatalf("operation journal: %+v ok=%v %v", stored, ok, err)
	}

	// A retry is idempotent: it reports the accepted operation and does not apply
	// a second time (which would also fail the revision CAS).
	before := record
	replay, err := control.ApplyMutation(raw, applyAuthority(public))
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if replay.Status != MutationAlreadyApplied {
		t.Fatalf("replay status: %+v", replay)
	}
	after, _, err := control.Get("policy", "p1")
	if err != nil || after.State != before.State || after.Revision != before.Revision ||
		string(after.Payload) != string(before.Payload) {
		t.Fatalf("replay changed the record: %+v -> %+v %v", before, after, err)
	}
}

func TestPromotedControlDomainRejectsUnsignedPut(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	cutover := promoteForApply(t, control, "policy")
	if err := control.Put(Record{
		Domain: "policy", ID: "unsigned", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"name":"unsigned"}`),
	}); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("unsigned write to promoted policy: %v", err)
	}
	if _, exists, err := control.Get("policy", "unsigned"); err != nil || exists {
		t.Fatalf("unsigned write persisted: exists=%v err=%v", exists, err)
	}
	_, public := rfc8032MutationKeys(t)
	raw := signedApply(t, cutover, public, repeatHex("e"), "signed", "ACTIVE", 1, map[string]any{"name": "signed"})
	if _, err := control.ApplyMutation(raw, applyAuthority(public)); err != nil {
		t.Fatalf("signed apply after refusal: %v", err)
	}
}

func TestApplyMutationCannotReportAnotherDomainAsApplied(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	policy := promoteForApply(t, control, "policy")
	authority := emptyPythonInventoryCheckpoint(t)
	dual, imported := importEmptyPythonSourceForClaimed(t, control, "target")
	current, err := signedTransition(t, control, CutoverTransition{
		Domain: "target", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: imported.TransferID, Authority: authority,
	})
	if err != nil {
		t.Fatal(err)
	}
	_, public := rfc8032MutationKeys(t)
	operationID := repeatHex("c")
	first := signedApply(t, policy, public, operationID, "p1", "ACTIVE", 1, map[string]any{"name": "same payload"})
	if _, err := control.ApplyMutation(first, applyAuthority(public)); err != nil {
		t.Fatal(err)
	}
	second := signedApply(t, current, public, operationID, "p1", "ACTIVE", 1, map[string]any{"name": "same payload"})
	if _, err := control.ApplyMutation(second, applyAuthority(public)); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("cross-domain operation reuse must conflict: %v", err)
	}
	if _, exists, err := control.Get("target", "p1"); err != nil || exists {
		t.Fatalf("target falsely reported an applied record: exists=%v %v", exists, err)
	}
	if stored, exists, err := control.GetOperation(operationID); err != nil || !exists || stored.Domain != "policy" {
		t.Fatalf("original operation changed: %+v exists=%v %v", stored, exists, err)
	}
}

// A signed request must not be able to overwrite an existing record: the revision
// CAS refuses a second apply that claims the same revision.
func TestApplyMutationRefusesAStaleRecordRevision(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	cutover := promoteForApply(t, control, "policy")
	_, public := rfc8032MutationKeys(t)
	first := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "first"})
	if _, err := control.ApplyMutation(first, applyAuthority(public)); err != nil {
		t.Fatal(err)
	}
	second := signedApply(t, cutover, public, repeatHex("d"), "p1", "DISABLED", 1, map[string]any{"name": "second"})
	if _, err := control.ApplyMutation(second, applyAuthority(public)); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("stale record revision: %v", err)
	}
	record, _, err := control.Get("policy", "p1")
	if err != nil || record.State != "ACTIVE" {
		t.Fatalf("refused apply changed the record: %+v %v", record, err)
	}
	if _, ok, err := control.GetOperation(repeatHex("d")); err != nil || ok {
		t.Fatalf("refused apply journaled an operation: ok=%v %v", ok, err)
	}
}

func TestApplyMutationRequiresDurableAuthority(t *testing.T) {
	t.Run("no cutover capability", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		_, public := rfc8032MutationKeys(t)
		cutover, err := control.GetCutover("policy")
		if err != nil {
			t.Fatal(err)
		}
		raw := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrCutoverNotAuthorized) {
			t.Fatalf("apply without the cutover capability: %v", err)
		}
	})

	t.Run("domain still Python's", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		_, public := rfc8032MutationKeys(t)
		cutover, err := control.GetCutover("policy")
		if err != nil {
			t.Fatal(err)
		}
		raw := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrCutoverNotAuthorized) {
			t.Fatalf("apply to an unpromoted domain: %v", err)
		}
		if _, exists, err := control.Get("policy", "p1"); err != nil || exists {
			t.Fatalf("refused apply wrote a record: exists=%v %v", exists, err)
		}
	})

	t.Run("fenced domain", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		cutover := promoteForApply(t, control, "action")
		_, public := rfc8032MutationKeys(t)
		raw := signedApply(t, cutover, public, repeatHex("c"), "act-1", "PENDING", 1, map[string]any{"name": "x"})
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrMutationRequestDomainFenced) {
			t.Fatalf("fenced domain: %v", err)
		}
	})
}
