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
)

func loadStorageOperationGrantFixture(t *testing.T) authorityRequestFixture {
	t.Helper()
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve storage operation grant test source")
	}
	path := filepath.Join(filepath.Dir(source), "..", "..", "..", "compat", "native-runtime", "v31", "control", "storage_operation_grant_vector.json")
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

func testStorageOperationGrantContext(t *testing.T, fixture authorityRequestFixture, overrides map[string]any) StorageOperationGrantContext {
	t.Helper()
	now, err := time.Parse(time.RFC3339, fixture.Now)
	if err != nil {
		t.Fatal(err)
	}
	context := StorageOperationGrantContext{
		Now:                  now,
		SignerPublicKey:      fixture.SignerPublicKey,
		SignerKeyID:          fixture.SignerKeyID,
		ExpectedDomain:       "action",
		ExpectedOperation:    StorageOperationGrantPut,
		ExpectedRuntime:      RuntimeGo,
		ExpectedMode:         ModeShadow,
		ExpectedFleetID:      "fleet-a",
		ExpectedEnvironment:  "test",
		ExpectedRole:         "control-plane",
		CurrentFencingToken:  4,
		LiveEpoch:            4,
		SeenRequestIDs:       map[string]bool{},
		SeenNonces:           map[string]bool{},
		SeenOperationDigests: map[string]string{},
		MaxFutureSkewSeconds: DefaultStorageOperationGrantSkew,
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
	if value, ok := overrides["expected_operation"].(string); ok {
		context.ExpectedOperation = value
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

func frozenStorageOperationCommand() StorageOperationCommand {
	return StorageOperationCommand{
		ActionID: "act-1", ExecutionEpoch: 4,
		OperationID:  "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		MutationType: "PUT_CHUNK", Provider: "s3",
		TargetIdentity: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
		Bucket:         "backup-a", Prefix: "native/", ObjectKey: "objects/chunk-1",
		ObjectDigest:   "039058c6f2c0cb492c533b0a4d14ef77cc0f78abccced5287d84a1a2011cfb81",
		ExpectedLength: 3, ConditionType: "CREATE_ONLY", ClaimRevision: 2,
	}
}

func TestVerifyStorageOperationGrantAcceptsFrozenVector(t *testing.T) {
	fixture := loadStorageOperationGrantFixture(t)
	document, err := VerifyStorageOperationGrant([]byte(fixture.CanonicalRequest), testStorageOperationGrantContext(t, fixture, nil))
	if err != nil {
		t.Fatal(err)
	}
	if asString(document["digest"]) != fixture.RequestDigest || asString(document["operation"]) != StorageOperationGrantPut {
		t.Fatalf("verified document: %+v", document)
	}
	if err := BindStorageOperationGrant(document, frozenStorageOperationCommand()); err != nil {
		t.Fatal(err)
	}
}

func TestVerifyStorageOperationGrantRejectsFrozenFailures(t *testing.T) {
	fixture := loadStorageOperationGrantFixture(t)
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
					digest, err := storageOperationGrantDigest(document)
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
			_, err := VerifyStorageOperationGrant(raw, testStorageOperationGrantContext(t, fixture, test.Context))
			if err == nil || err.Error() != test.Error {
				t.Fatalf("error=%v want %s", err, test.Error)
			}
		})
	}
}

func TestStorageOperationGrantIdempotentOperationAndCommandBinding(t *testing.T) {
	fixture := loadStorageOperationGrantFixture(t)
	document, err := VerifyStorageOperationGrant([]byte(fixture.CanonicalRequest), testStorageOperationGrantContext(t, fixture, nil))
	if err != nil {
		t.Fatal(err)
	}
	context := testStorageOperationGrantContext(t, fixture, nil)
	context.SeenOperationDigests[asString(document["operationId"])] = asString(document["payloadDigest"])
	if _, err := VerifyStorageOperationGrant([]byte(fixture.CanonicalRequest), context); err != nil {
		t.Fatalf("same operationId and payload must verify: %v", err)
	}
	command := frozenStorageOperationCommand()
	command.ObjectKey = "objects/other"
	if err := BindStorageOperationGrant(document, command); !errors.Is(err, ErrStorageOperationGrantCommandMismatch) {
		t.Fatalf("substituted object key: %v", err)
	}
	command = frozenStorageOperationCommand()
	command.ExpectedLength = 4
	if err := BindStorageOperationGrant(document, command); !errors.Is(err, ErrStorageOperationGrantCommandMismatch) {
		t.Fatalf("substituted length: %v", err)
	}
	command = frozenStorageOperationCommand()
	command.ExecutionEpoch = 5
	if err := BindStorageOperationGrant(document, command); !errors.Is(err, ErrStorageOperationGrantCommandMismatch) {
		t.Fatalf("substituted epoch: %v", err)
	}
}

func TestSignStorageOperationGrantRoundTripMatchesVerifier(t *testing.T) {
	private, public := rfc8032Signer(t)
	if _, _, err := SignStorageOperationGrant(frozenStorageOperationGrantUnsigned(), private, "not-a-key"); !errors.Is(err, ErrStorageOperationGrantSignerMismatch) {
		t.Fatalf("bad public key: %v", err)
	}
	_, raw, err := SignStorageOperationGrant(frozenStorageOperationGrantUnsigned(), private, public)
	if err != nil {
		t.Fatal(err)
	}
	fixture := loadStorageOperationGrantFixture(t)
	if string(raw) != fixture.CanonicalRequest {
		t.Fatalf("signed grant drifted from frozen vector")
	}
	if _, err := VerifyStorageOperationGrant(raw, testStorageOperationGrantContext(t, fixture, nil)); err != nil {
		t.Fatalf("signed document must verify: %v", err)
	}
}

func TestStorageOperationGrantHelpersFailClosed(t *testing.T) {
	if _, err := VerifyStorageOperationGrant(nil, StorageOperationGrantContext{}); !errors.Is(err, ErrStorageOperationGrantInvalid) {
		t.Fatalf("empty: %v", err)
	}
	if _, err := VerifyStorageOperationGrant(make([]byte, MaxStorageOperationGrantBytes+1), StorageOperationGrantContext{}); !errors.Is(err, ErrStorageOperationGrantTooLarge) {
		t.Fatalf("too large: %v", err)
	}
	if _, _, err := SignStorageOperationGrant(nil, nil, ""); !errors.Is(err, ErrStorageOperationGrantInvalid) {
		t.Fatalf("nil unsigned: %v", err)
	}
	if _, _, err := SignStorageOperationGrant(map[string]any{"signature": "x"}, nil, ""); !errors.Is(err, ErrStorageOperationGrantInvalid) {
		t.Fatalf("pre-signed: %v", err)
	}
	if err := BindStorageOperationGrant(map[string]any{}, frozenStorageOperationCommand()); !errors.Is(err, ErrStorageOperationGrantInvalid) {
		t.Fatalf("empty bind: %v", err)
	}
}

func TestStorageOperationGrantRejectsMutationRequestAndNonCanonicalJSON(t *testing.T) {
	fixture := loadStorageOperationGrantFixture(t)
	mutation := loadMutationRequestFixture(t)
	if _, err := VerifyStorageOperationGrant([]byte(mutation.CanonicalRequest), testStorageOperationGrantContext(t, fixture, nil)); !errors.Is(err, ErrStorageOperationGrantSchemaInvalid) {
		t.Fatalf("mutation request adopted as grant: %v", err)
	}
	indented, err := json.MarshalIndent(map[string]any{"schema": StorageOperationGrantSchema}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyStorageOperationGrant(indented, testStorageOperationGrantContext(t, fixture, nil)); !errors.Is(err, ErrStorageOperationGrantCanonicalMismatch) && !errors.Is(err, ErrStorageOperationGrantFieldsInvalid) {
		t.Fatalf("pretty JSON: %v", err)
	}
}
