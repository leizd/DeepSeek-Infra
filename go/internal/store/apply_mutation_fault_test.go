package store

import (
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

// Direct unit coverage for the two recursive validators, whose list and depth
// branches a JSON document cannot reach through the wire alone.
func TestMutationRecordPayloadValidators(t *testing.T) {
	if err := validateMutationRecordPayload(map[string]any{"ok": json.Number("1")}, 0); err != nil {
		t.Fatalf("integer body: %v", err)
	}
	if err := validateMutationRecordPayload([]any{"a", nil, true}, 0); err != nil {
		t.Fatalf("list body: %v", err)
	}
	if err := validateMutationRecordPayload(1.5, 0); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("float value: %v", err)
	}
	if err := validateMutationRecordPayload([]any{json.Number("1.5")}, 0); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("float inside a list: %v", err)
	}
	if err := validateMutationRecordPayload(map[string]any{"x": json.Number("1e3")}, 0); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("exponent number: %v", err)
	}
	if err := validateMutationRecordPayload("x", maxRecordPayloadDepth+1); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("depth bound: %v", err)
	}

	if err := rejectMutationBodySecretKeys(map[string]any{"name": "x"}, 0); err != nil {
		t.Fatalf("clean body: %v", err)
	}
	if err := rejectMutationBodySecretKeys(map[string]any{"outer": map[string]any{"privateKey": "x"}}, 0); !errors.Is(err, ErrMutationRequestSecretDetected) {
		t.Fatalf("nested secret key: %v", err)
	}
	if err := rejectMutationBodySecretKeys([]any{map[string]any{"apiKey": "x"}}, 0); !errors.Is(err, ErrMutationRequestSecretDetected) {
		t.Fatalf("secret key inside a list: %v", err)
	}
	if err := rejectMutationBodySecretKeys([]any{"clean"}, 0); err != nil {
		t.Fatalf("clean list: %v", err)
	}
	if err := rejectMutationBodySecretKeys("x", maxRecordPayloadDepth+1); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("depth bound: %v", err)
	}

	// The oracle's exemption set is exactly the four envelope-adjacent names, and
	// the safe-suffix exemption the shared control-record rule has does NOT apply.
	for _, exempt := range []string{"fencingToken", "signature", "signatureAlgorithm", "signerKeyId"} {
		if mutationChannelSecretKey(exempt) {
			t.Fatalf("%s must stay exempt", exempt)
		}
	}
	for _, flagged := range []string{"myTokenDigest", "credentialReference", "mySecretRef", "passwordId"} {
		if !mutationChannelSecretKey(flagged) {
			t.Fatalf("%s must be refused by the mutation-channel rule", flagged)
		}
	}
}

func TestApplyMutationFaultsFailClosed(t *testing.T) {
	_, public := rfc8032MutationKeys(t)
	request := func(t *testing.T, control *Control) []byte {
		t.Helper()
		cutover, err := control.GetCutover("policy")
		if err != nil {
			t.Fatal(err)
		}
		return signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
	}

	t.Run("closed store", func(t *testing.T) {
		control := openAuthority(t)
		raw := request(t, control)
		control.closed = true
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("closed store: %v", err)
		}
		control.closed = false
		if err := control.Close(); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("inactive schema", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		raw := request(t, control)
		control.schema = SchemaV8
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrSchemaInactive) {
			t.Fatalf("inactive schema: %v", err)
		}
		control.schema = CurrentSchema
	})

	t.Run("unavailable database", func(t *testing.T) {
		control := openAuthority(t)
		raw := request(t, control)
		if err := control.db.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); err == nil {
			t.Fatal("closed database must refuse")
		}
		_ = control.Close()
	})

	t.Run("stale writer fence", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		raw := request(t, control)
		if _, err := control.db.Exec("UPDATE control_writer SET owner_instance_id = 'someone-else' WHERE singleton = 1"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("stale writer fence: %v", err)
		}
	})

	t.Run("broken schema object", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		raw := request(t, control)
		if _, err := control.db.Exec("DROP TRIGGER control_authority_checkpoints_no_delete"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("broken schema: %v", err)
		}
	})

	t.Run("malformed request", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, err := control.ApplyMutation([]byte("{"), applyAuthority(public)); !errors.Is(err, ErrMutationRequestInvalid) {
			t.Fatalf("malformed request: %v", err)
		}
	})

	t.Run("unknown domain", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, err := control.ApplyMutation([]byte(`{"domain":"nonexistent"}`), applyAuthority(public)); !errors.Is(err, ErrUnknownDomain) {
			t.Fatalf("unknown domain: %v", err)
		}
	})

	t.Run("corrupt cutover row", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		// The cutover row has a delete trigger but no update trigger; a transfer id
		// that is not a legal record id is corruption, not a refusal.
		if _, err := control.db.Exec("UPDATE control_cutover SET transfer_id = '..' WHERE domain = 'policy'"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyMutation([]byte(`{"domain":"policy"}`), applyAuthority(public)); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt cutover row: %v", err)
		}
	})

	t.Run("authority clock from the store", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		cutover := promoteForApply(t, control, "policy")
		raw := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
		// A zero Now falls back to the store clock, which is the Unix epoch here,
		// so a 2026 request is far in the future.
		if _, err := control.ApplyMutation(raw, MutationAuthority{
			SignerPublicKey: public, FleetID: "fleet-a", Environment: "test",
		}); !errors.Is(err, ErrMutationRequestFutureSkew) {
			t.Fatalf("store clock skew: %v", err)
		}
	})

	t.Run("unusable signer key", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		cutover := promoteForApply(t, control, "policy")
		raw := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
		if _, err := control.ApplyMutation(raw, MutationAuthority{
			SignerPublicKey: "not-a-key", FleetID: "fleet-a", Environment: "test",
			Now: time.Date(2026, 9, 5, 0, 0, 40, 0, time.UTC),
		}); !errors.Is(err, ErrMutationRequestSignerMismatch) {
			t.Fatalf("unusable signer key: %v", err)
		}
	})

	t.Run("tampered request", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		cutover := promoteForApply(t, control, "policy")
		raw := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
		var document map[string]any
		if err := decodeSingleJSON(raw, &document); err != nil {
			t.Fatal(err)
		}
		document["payloadDigest"] = "sha256:" + repeatHex("0")
		tampered, err := canonicalAuthorityJSON(document)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyMutation(tampered, applyAuthority(public)); !errors.Is(err, ErrMutationRequestPayloadDigestMismatch) {
			t.Fatalf("tampered request: %v", err)
		}
	})

	t.Run("stale cutover revision", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		cutover := promoteForApply(t, control, "policy")
		stale := cutover
		stale.Revision--
		raw := signedApply(t, stale, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrRevisionConflict) {
			t.Fatalf("stale cutover revision: %v", err)
		}
	})

	t.Run("replay conflict", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		cutover := promoteForApply(t, control, "policy")
		first := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "first"})
		if _, err := control.ApplyMutation(first, applyAuthority(public)); err != nil {
			t.Fatal(err)
		}
		// Same operation, different body: a replay would silently change what the
		// operation meant, so it is a conflict rather than an idempotent retry.
		second := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "second"})
		if _, err := control.ApplyMutation(second, applyAuthority(public)); !errors.Is(err, ErrMutationRequestReplayConflict) {
			t.Fatalf("replay conflict: %v", err)
		}
	})

	t.Run("writer lease expires before commit", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		cutover := promoteForApply(t, control, "policy")
		raw := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
		// The apply reads the clock twice: once to assert the writer fence and once
		// before committing. Only the second read may fall outside the lease, and
		// the record must not survive it.
		calls := 0
		control.now = func() int64 {
			calls++
			if calls == 1 {
				return 1000
			}
			return 1031
		}
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("lease expiry before commit: %v", err)
		}
		if _, exists, err := control.Get("policy", "p1"); err != nil || exists {
			t.Fatalf("lease expiry left a record: exists=%v %v", exists, err)
		}
	})

	t.Run("journal insert rejected rolls the record back", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		cutover := promoteForApply(t, control, "policy")
		raw := signedApply(t, cutover, public, repeatHex("c"), "p1", "ACTIVE", 1, map[string]any{"name": "x"})
		if _, err := control.db.Exec(`CREATE TRIGGER reject_operation_insert
			BEFORE INSERT ON control_operations
			BEGIN SELECT RAISE(ABORT, 'reject operation insert'); END`); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyMutation(raw, applyAuthority(public)); err == nil {
			t.Fatal("rejected journal insert must fail the apply")
		}
		// The record write and the journal row are one transaction: neither may
		// survive without the other, or a retry would apply the mutation twice.
		if _, exists, err := control.Get("policy", "p1"); err != nil || exists {
			t.Fatalf("rejected journal insert left a record: exists=%v %v", exists, err)
		}
		if _, exists, err := control.GetOperation(repeatHex("c")); err != nil || exists {
			t.Fatalf("rejected journal insert left an operation: exists=%v %v", exists, err)
		}
	})
}

// The v1 accept path and the operation reader share the same store plumbing, so
// their connection failures are exercised here too.
func TestOperationJournalConnectionFailuresFailClosed(t *testing.T) {
	_, public := rfc8032MutationKeys(t)
	now := time.Unix(1000, 0).UTC()

	t.Run("accept on a closed database", func(t *testing.T) {
		control := openControlAt(t, 1000)
		raw := signPolicyMutation(t, control, mustPrivateKey(t, public), public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil)
		if err := control.db.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := control.AcceptMutation(raw, mutationAuth(public, now)); err == nil {
			t.Fatal("accept on a closed database must fail")
		}
		_ = control.Close()
	})

	t.Run("lookup on a closed database", func(t *testing.T) {
		control := openControlAt(t, 1000)
		if err := control.db.Close(); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.GetOperation(hex64(0x33)); err == nil {
			t.Fatal("lookup on a closed database must fail")
		}
		_ = control.Close()
	})
}

func mustPrivateKey(t *testing.T, public string) ed25519.PrivateKey {
	t.Helper()
	private, derived := rfc8032MutationKeys(t)
	if derived != public {
		t.Fatalf("public key mismatch: %s vs %s", derived, public)
	}
	return private
}
