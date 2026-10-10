package store

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// promotedPolicyStore opens an authority-enabled store and durably promotes the policy
// domain, which is the only state in which an operator mutation is allowed to write.
func promotedPolicyStore(t *testing.T) *Control {
	t.Helper()
	control := openAuthority(t)
	t.Cleanup(func() { _ = control.Close() })
	checkpoint, dual, imported := importEmptyPythonSource(t, control, "policy")
	if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil {
		t.Fatalf("promote policy: %v", err)
	}
	return control
}

func policyRecord(id string, revision int64, extra map[string]any) Record {
	payload := map[string]any{"policyId": id, "policyRevision": revision}
	for key, value := range extra {
		payload[key] = value
	}
	encoded, _ := json.Marshal(payload)
	return Record{Domain: "policy", ID: id, Revision: revision, State: "ACTIVE", Payload: encoded}
}

func operatorMutation(operationID, recordID string, revision int64) OperatorMutation {
	return OperatorMutation{
		OperationID: operationID,
		ActionID:    "action_" + operationID,
		Actor:       "operator:test-session",
		Record:      policyRecord(recordID, revision, map[string]any{"name": "nightly"}),
	}
}

// The whole point of the channel: an authenticated operator writes a control record, and
// the record, its event and its provenance are one durable fact.
func TestOperatorMutationWritesRecordEventAndJournalTogether(t *testing.T) {
	control := promotedPolicyStore(t)
	before := countControlEvents(t, control)

	result, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("a"), "p-op", 1))
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if result.Status != OperatorMutationApplied || result.Domain != "policy" || result.Revision != 1 {
		t.Fatalf("result: %+v", result)
	}
	if result.ActionID != "action_"+repeatHex("a") || result.Actor != "operator:test-session" {
		t.Fatalf("result identity: %+v", result)
	}
	// The epoch comes from the live cutover, never from the caller.
	cutover, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if result.ExecutionEpoch != uint64(cutover.Epoch) || result.CutoverRevision != cutover.Revision ||
		result.FencingToken != cutover.FencingToken {
		t.Fatalf("result authority: %+v cutover=%+v", result, cutover)
	}

	record, exists, err := control.Get("policy", "p-op")
	if err != nil || !exists || record.Revision != 1 || record.State != "ACTIVE" {
		t.Fatalf("record: %+v %v %v", record, exists, err)
	}
	if after := countControlEvents(t, control); after != before+1 {
		t.Fatalf("the write must append exactly one control event: %d -> %d", before, after)
	}

	journal, found, err := control.ReadOperatorMutation(repeatHex("a"))
	if err != nil || !found {
		t.Fatalf("journal read: %v %v", found, err)
	}
	if journal.RequestDigest != result.RequestDigest || journal.PayloadDigest != result.PayloadDigest {
		t.Fatalf("journal digests: %+v", journal)
	}
	recordedAt, found, err := control.OperatorMutationRecordedAt(repeatHex("a"))
	if err != nil || !found || recordedAt.Unix() != 1000 {
		t.Fatalf("recordedAt: %v %v %v", recordedAt, found, err)
	}
	if _, found, err := control.OperatorMutationRecordedAt(repeatHex("f")); err != nil || found {
		t.Fatalf("unknown operation must not be found: %v %v", found, err)
	}
	if _, found, err := control.ReadOperatorMutation(repeatHex("f")); err != nil || found {
		t.Fatalf("unknown read: %v %v", found, err)
	}

	// The signed journal is untouched: an operator mutation is not a signed mutation.
	var operations int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_operations").Scan(&operations); err != nil {
		t.Fatal(err)
	}
	if operations != 0 {
		t.Fatalf("the operator channel must not write control_operations: %d", operations)
	}
}

// A retry of the same request is a replay; reusing the key for a different body is a
// conflict. Either way the record is written once.
func TestOperatorMutationIsIdempotentAndRefusesAReusedKey(t *testing.T) {
	control := promotedPolicyStore(t)
	mutation := operatorMutation(repeatHex("b"), "p-replay", 1)
	first, err := control.ApplyOperatorMutation(mutation)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := control.ApplyOperatorMutation(mutation)
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if replay.Status != OperatorMutationAlreadyApplied || replay.RequestDigest != first.RequestDigest {
		t.Fatalf("replay: %+v", replay)
	}
	if rows := countOperatorMutations(t, control); rows != 1 {
		t.Fatalf("a replay must not add a journal row: %d", rows)
	}
	record, _, err := control.Get("policy", "p-replay")
	if err != nil || record.Revision != 1 {
		t.Fatalf("a replay must not advance the record: %+v %v", record, err)
	}

	conflict := mutation
	conflict.Record = policyRecord("p-replay", 2, map[string]any{"name": "different"})
	if _, err := control.ApplyOperatorMutation(conflict); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("reused key with a new body: %v", err)
	}
	if rows := countOperatorMutations(t, control); rows != 1 {
		t.Fatalf("a conflict must not add a journal row: %d", rows)
	}
}

func TestOperatorMutationRefusals(t *testing.T) {
	t.Run("no deployment capability", func(t *testing.T) {
		control, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "no-capability"})
		if err != nil {
			t.Fatal(err)
		}
		defer control.Close()
		if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("c"), "p", 1)); !errors.Is(err, ErrCutoverNotAuthorized) {
			t.Fatalf("want CUTOVER_NOT_AUTHORIZED, got %v", err)
		}
	})
	t.Run("domain not promoted", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("d"), "p", 1)); !errors.Is(err, ErrCutoverNotAuthorized) {
			t.Fatalf("want CUTOVER_NOT_AUTHORIZED, got %v", err)
		}
		if rows := countOperatorMutations(t, control); rows != 0 {
			t.Fatalf("a refused write must leave no journal row: %d", rows)
		}
	})
	t.Run("fenced domain", func(t *testing.T) {
		control := promotedPolicyStore(t)
		for _, domain := range []string{"action", "scheduler_run", "wave", "transfer"} {
			mutation := operatorMutation(repeatHex("e"), "fenced", 1)
			mutation.Record.Domain = domain
			if _, err := control.ApplyOperatorMutation(mutation); !errors.Is(err, ErrMutationRequestDomainFenced) {
				t.Fatalf("%s: want MUTATION_REQUEST_DOMAIN_FENCED, got %v", domain, err)
			}
		}
	})
	t.Run("unknown domain", func(t *testing.T) {
		control := promotedPolicyStore(t)
		mutation := operatorMutation(repeatHex("f"), "p", 1)
		mutation.Record.Domain = "not-a-domain"
		if _, err := control.ApplyOperatorMutation(mutation); !errors.Is(err, ErrUnknownDomain) {
			t.Fatalf("want UNKNOWN_DOMAIN, got %v", err)
		}
	})
	t.Run("missing identity", func(t *testing.T) {
		control := promotedPolicyStore(t)
		for name, mutation := range map[string]OperatorMutation{
			"operation id": {ActionID: "a", Actor: "actor", Record: policyRecord("p", 1, nil)},
			"action id":    {OperationID: "o", Actor: "actor", Record: policyRecord("p", 1, nil)},
			"actor":        {OperationID: "o", ActionID: "a", Record: policyRecord("p", 1, nil)},
		} {
			if _, err := control.ApplyOperatorMutation(mutation); !errors.Is(err, ErrMutationRequestInvalid) {
				t.Fatalf("%s: want MUTATION_REQUEST_INVALID, got %v", name, err)
			}
		}
	})
	t.Run("record admission", func(t *testing.T) {
		control := promotedPolicyStore(t)
		invalid := operatorMutation(repeatHex("a"), "..", 1)
		if _, err := control.ApplyOperatorMutation(invalid); !errors.Is(err, ErrEmptyRecordID) {
			t.Fatalf("want EMPTY_RECORD_ID, got %v", err)
		}
		secret := operatorMutation(repeatHex("b"), "p-secret", 1)
		secret.Record.Payload = json.RawMessage(`{"policyId":"p-secret","policyRevision":1,"apiKey":"sk-live-123"}`)
		if _, err := control.ApplyOperatorMutation(secret); err == nil {
			t.Fatal("a credential-bearing record key must be refused")
		}
		// A `credentialRef` names a handle rather than a secret and is exactly what a
		// stored policy carries, so it must stay writable.
		handle := operatorMutation(repeatHex("e"), "p-handle", 1)
		handle.Record.Payload = json.RawMessage(`{"policyId":"p-handle","policyRevision":1,"recoveryDrill":{"credentialRef":"env:DRILL"}}`)
		if _, err := control.ApplyOperatorMutation(handle); err != nil {
			t.Fatalf("a credential reference must be writable: %v", err)
		}
		// A float is *not* refused here: a stored policy always carries float percentages,
		// so the signed channel's primitive-set rule would make this domain unwritable. The
		// secret-key rule still applies.
		float := operatorMutation(repeatHex("c"), "p-float", 1)
		float.Record.Payload = json.RawMessage(`{"policyId":"p-float","policyRevision":1,"ratio":0.5}`)
		if _, err := control.ApplyOperatorMutation(float); err != nil {
			t.Fatalf("a policy document with float percentages must be writable: %v", err)
		}
		if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("d"), "p-gap", 4)); !errors.Is(err, ErrRevisionConflict) {
			t.Fatalf("a skipped revision: %v", err)
		}
	})
	t.Run("closed store", func(t *testing.T) {
		control := promotedPolicyStore(t)
		if err := control.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("e"), "p", 1)); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("want WRITER_FENCE_HELD, got %v", err)
		}
		if _, _, err := control.ReadOperatorMutation("x"); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("read on a closed store: %v", err)
		}
		if _, _, err := control.OperatorMutationRecordedAt("x"); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("recordedAt on a closed store: %v", err)
		}
	})
}

// The journal is append-only: an applied operator mutation cannot be rewritten or
// removed afterwards, which is what makes it usable as an audit record.
func TestOperatorMutationJournalIsAppendOnly(t *testing.T) {
	control := promotedPolicyStore(t)
	if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("a"), "p-immutable", 1)); err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"UPDATE control_operator_mutations SET actor='someone-else'",
		"DELETE FROM control_operator_mutations",
	} {
		if _, err := control.db.Exec(statement); err == nil ||
			!strings.Contains(err.Error(), "OPERATOR_MUTATION_IMMUTABLE") {
			t.Fatalf("%s must be refused by the immutability trigger: %v", statement, err)
		}
	}
}

// A rollback to schema 0 discards the store. It must refuse while the operator journal
// holds anything, and must remove the table when it does not.
//
// The retention case writes its journal row directly: a *promoted* store cannot reach
// `Rollback(0)` at all (its promotion history is retained first), so the guard is pinned
// where it is reachable rather than through a promotion that refuses earlier for an
// unrelated reason.
func TestOperatorMutationRollbackRespectsTheJournal(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, err := control.db.Exec(`INSERT INTO ` + operatorMutationTable + `(
		operation_id, domain, record_id, record_revision, record_state, payload_digest,
		request_digest, action_id, execution_epoch, cutover_revision, fencing_token,
		writer_fencing_token, actor, recorded_at
	) VALUES('op-rollback','policy','p',1,'ACTIVE',
		'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
		'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
		'action-1',0,0,1,1,'operator:test',1000)`); err != nil {
		t.Fatal(err)
	}
	if err := control.Rollback(0); !errors.Is(err, ErrOperatorMutationHistoryRetained) {
		t.Fatalf("want OPERATOR_MUTATION_HISTORY_RETAINED, got %v", err)
	}
	if rows := countOperatorMutations(t, control); rows != 1 {
		t.Fatalf("a refused rollback must not drop the journal: %d", rows)
	}

	empty := openAuthority(t)
	defer empty.Close()
	if err := empty.Rollback(0); err != nil {
		t.Fatalf("an empty journal must not block a rollback: %v", err)
	}
	var marker int
	if err := empty.db.QueryRow(
		"SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", operatorMutationTable,
	).Scan(&marker); err == nil {
		t.Fatal("the operator journal must be dropped")
	}
}

// A v14 store upgrades to v15 without losing anything and with the new journal present.
func TestV15MigrationPreservesRecordsAndVerifiesTheJournal(t *testing.T) {
	control := promotedPolicyStore(t)
	if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("a"), "p-before", 1)); err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}

	// Materialize the v14 shape: drop the v15 objects and lower the metadata ceiling.
	control, err := OpenControl(OpenOptions{Path: path, Owner: "v15-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"DROP TABLE control_operator_mutations",
		`CREATE TABLE meta_v14_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 14),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO meta_v14_fixture SELECT singleton,runtime,mode,14,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE meta_v14_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=15",
		"PRAGMA user_version=14",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatalf("%s: %v", statement, err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "v15-successor", AuthorizeCutover: true})
	if err != nil {
		t.Fatalf("v14 store did not upgrade: %v", err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("schema after upgrade: %d", reopened.SchemaVersion())
	}
	if record, exists, err := reopened.Get("policy", "p-before"); err != nil || !exists || record.Revision != 1 {
		t.Fatalf("the upgrade lost the record: %+v %v %v", record, exists, err)
	}
	// The journal was dropped by the fixture, so the upgrade starts empty and a fresh
	// operator mutation must work on the migrated store.
	if rows := countOperatorMutations(t, reopened); rows != 0 {
		t.Fatalf("upgraded journal should be empty: %d", rows)
	}
	cutoverAfter, cutoverErr := reopened.GetCutover("policy")
	if _, err := reopened.ApplyOperatorMutation(operatorMutation(repeatHex("b"), "p-after", 1)); err != nil {
		t.Fatalf("write after upgrade: %v (cutover=%+v err=%v)", err, cutoverAfter, cutoverErr)
	}
}

// A store whose operator journal was altered underneath it is not this runtime's store.
func TestOperatorMutationSchemaIsVerifiedOnOpen(t *testing.T) {
	control := promotedPolicyStore(t)
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	control, err := OpenControl(OpenOptions{Path: path, Owner: "tamper"})
	if err != nil {
		t.Fatal(err)
	}
	// Drop an immutability trigger: the object catalog no longer matches.
	if _, err := control.db.Exec("DROP TRIGGER control_operator_mutations_no_update"); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: path, Owner: "tampered"}); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("a tampered journal must fail closed: %v", err)
	}
}

// A tombstone is terminal and invisible: the id is gone from the authoritative inventory,
// the row and its immutable events stay, and nothing can leave the state or reuse the id.
func TestTombstoneIsTerminalAndHiddenFromTheInventory(t *testing.T) {
	control := promotedPolicyStore(t)
	if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("a"), "p-tombstone", 1)); err != nil {
		t.Fatal(err)
	}
	events := countControlEvents(t, control)
	tombstone := operatorMutation(repeatHex("b"), "p-tombstone", 2)
	tombstone.Record.State = TombstoneState
	if result, err := control.ApplyOperatorMutation(tombstone); err != nil || result.State != TombstoneState {
		t.Fatalf("tombstone write: %+v %v", result, err)
	}

	records, err := control.ListAuthoritativeRecords("policy")
	if err != nil {
		t.Fatalf("the inventory must stay readable across a tombstone: %v", err)
	}
	if len(records) != 0 {
		t.Fatalf("a tombstoned record must be absent from the inventory: %+v", records)
	}
	// The row is still there, with its events, which is what keeps the store consistent.
	record, exists, err := control.Get("policy", "p-tombstone")
	if err != nil || !exists || record.State != TombstoneState || record.Revision != 2 {
		t.Fatalf("tombstone row: %+v %v %v", record, exists, err)
	}
	if after := countControlEvents(t, control); after != events+1 {
		t.Fatalf("the tombstone must append one event: %d -> %d", events, after)
	}

	// Nothing can leave the terminal state, and the id cannot be reused: every attempt is
	// a transition *out of* DELETED, which the state table does not define.
	revive := operatorMutation(repeatHex("c"), "p-tombstone", 3)
	if _, err := control.ApplyOperatorMutation(revive); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("a tombstone must not move: %v", err)
	}
	reuse := operatorMutation(repeatHex("d"), "p-tombstone", 1)
	if _, err := control.ApplyOperatorMutation(reuse); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("a tombstoned id must not be recreated: %v", err)
	}
	// A second tombstone is the same move, and is refused for the same reason.
	again := operatorMutation(repeatHex("e"), "p-tombstone", 3)
	again.Record.State = TombstoneState
	if _, err := control.ApplyOperatorMutation(again); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("a second delete must be refused: %v", err)
	}
}

// The operator journal's size is readable for an operator console, and refuses on a store
// it cannot vouch for.
func TestOperatorJournalSizeRefusals(t *testing.T) {
	control := promotedPolicyStore(t)
	if size, err := control.OperatorJournalSize(); err != nil || size != 0 {
		t.Fatalf("empty journal: %d %v", size, err)
	}
	if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("a"), "p-size", 1)); err != nil {
		t.Fatal(err)
	}
	if size, err := control.OperatorJournalSize(); err != nil || size != 1 {
		t.Fatalf("journal size: %d %v", size, err)
	}
	control.schema = SchemaV14
	if _, err := control.OperatorJournalSize(); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("want SCHEMA_INACTIVE, got %v", err)
	}
	control.schema = CurrentSchema
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.OperatorJournalSize(); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("want WRITER_FENCE_HELD, got %v", err)
	}
}

func countOperatorMutations(t *testing.T, control *Control) int {
	t.Helper()
	var rows int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM " + operatorMutationTable).Scan(&rows); err != nil {
		t.Fatalf("count operator mutations: %v", err)
	}
	return rows
}

func countControlEvents(t *testing.T, control *Control) int {
	t.Helper()
	var rows int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_events").Scan(&rows); err != nil {
		t.Fatalf("count control events: %v", err)
	}
	return rows
}

// sqlRowError keeps the "no rows" expectation readable at the call site.
func sqlRowError(err error) error {
	if err == nil {
		return errors.New("row present")
	}
	return err
}

// A record written by the operator channel keeps the same canonical body and digest
// shape the signed channel uses, so a reader cannot tell the two apart by looking at the
// record — only by looking at which journal carries it.
func TestOperatorAndSignedChannelsKeepSeparateJournals(t *testing.T) {
	control := promotedPolicyStore(t)
	if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("a"), "p-operator", 1)); err != nil {
		t.Fatal(err)
	}
	private, public := rfc8032MutationKeys(t)
	cutover, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	raw := signedApply(t, cutover, public, repeatHex("b"), "p-signed", "ACTIVE", 1, map[string]any{
		"policyId": "p-signed", "policyRevision": json.Number("1"), "name": "signed",
	})
	if _, err := control.ApplyMutation(raw, applyAuthority(public)); err != nil {
		t.Fatalf("signed apply: %v", err)
	}
	_ = private
	records, err := control.ListAuthoritativeRecords("policy")
	if err != nil || len(records) != 2 {
		t.Fatalf("both channels must land in the same authoritative inventory: %+v %v", records, err)
	}
	var signed, operator int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_operations").Scan(&signed); err != nil {
		t.Fatal(err)
	}
	if err := control.db.QueryRow("SELECT COUNT(*) FROM " + operatorMutationTable).Scan(&operator); err != nil {
		t.Fatal(err)
	}
	if signed != 1 || operator != 1 {
		t.Fatalf("journals must stay separate: signed=%d operator=%d", signed, operator)
	}
}

// The journal records the writer fence it was admitted under, so a reconciliation can
// tell which writer instance owned the store.
func TestOperatorMutationRecordsTheWriterFence(t *testing.T) {
	control := promotedPolicyStore(t)
	if _, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("a"), "p-fence", 1)); err != nil {
		t.Fatal(err)
	}
	var fence int64
	if err := control.db.QueryRow(
		"SELECT writer_fencing_token FROM "+operatorMutationTable+" WHERE operation_id = ?", repeatHex("a"),
	).Scan(&fence); err != nil {
		t.Fatal(err)
	}
	if fence != control.Writer().FencingToken {
		t.Fatalf("journal fence %d != writer fence %d", fence, control.Writer().FencingToken)
	}
	if fence < 1 {
		t.Fatalf("fence must be positive: %d", fence)
	}
	// A schema the store does not recognise refuses the operator reads rather than
	// answering from a store it cannot verify.
	control.schema = SchemaV14
	if _, _, err := control.ReadOperatorMutation(repeatHex("a")); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("want SCHEMA_INACTIVE, got %v", err)
	}
	if _, _, err := control.OperatorMutationRecordedAt(repeatHex("a")); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("want SCHEMA_INACTIVE, got %v", err)
	}
	control.schema = CurrentSchema
}

// A journal row is the only record of an apply whose response was lost; asking for an
// operation id that was never applied is a miss, not an error.
func TestOperatorMutationReadOfAnUnknownOperationIsAMiss(t *testing.T) {
	control := promotedPolicyStore(t)
	result, found, err := control.ReadOperatorMutation(strings.Repeat("9", 64))
	if err != nil || found {
		t.Fatalf("unknown operation: %+v %v %v", result, found, err)
	}
	when, found, err := control.OperatorMutationRecordedAt(strings.Repeat("9", 64))
	if err != nil || found || !when.IsZero() {
		t.Fatalf("unknown recordedAt: %v %v %v", when, found, err)
	}
}

// A record body that is not an object cannot be digested, so the mutation is refused
// before anything is written.
func TestOperatorMutationRefusesAnUndigestibleRecordBody(t *testing.T) {
	control := promotedPolicyStore(t)
	for name, payload := range map[string]string{
		"not json":      "{not json",
		"not an object": "[]",
	} {
		mutation := operatorMutation(repeatHex("a"), "p-digest", 1)
		mutation.Record.Payload = json.RawMessage(payload)
		if _, err := control.ApplyOperatorMutation(mutation); err == nil {
			t.Fatalf("%s: payload must be refused", name)
		}
	}
	if rows := countOperatorMutations(t, control); rows != 0 {
		t.Fatalf("refused writes must leave no journal row: %d", rows)
	}
}

// Two stores cannot share one journal: the operation id is the idempotency key, and the
// replay path must compare the whole request, not just the id.
func TestOperatorMutationReplayDigestCoversTheWholeRequest(t *testing.T) {
	control := promotedPolicyStore(t)
	first, err := control.ApplyOperatorMutation(operatorMutation(repeatHex("a"), "p-digest-1", 1))
	if err != nil {
		t.Fatal(err)
	}
	second := operatorMutation(repeatHex("a"), "p-digest-2", 2)
	if _, err := control.ApplyOperatorMutation(second); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("want MUTATION_REQUEST_REPLAY_CONFLICT, got %v", err)
	}
	// A new operation id for the same record must take the *next* revision: skipping one
	// is a conflict, and the legal step (revision 2, DISABLED) is accepted.
	third := operatorMutation(repeatHex("c"), "p-digest-1", 3)
	third.Record.State = "DISABLED"
	if _, err := control.ApplyOperatorMutation(third); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("want REVISION_CONFLICT, got %v", err)
	}
	fourth := operatorMutation(repeatHex("d"), "p-digest-1", 2)
	fourth.Record.State = "DISABLED"
	if _, err := control.ApplyOperatorMutation(fourth); err != nil {
		t.Fatalf("the next revision must be accepted: %v", err)
	}
	if first.RequestDigest == "" || len(first.RequestDigest) != 64 {
		t.Fatalf("request digest: %q", first.RequestDigest)
	}
	if len(first.PayloadDigest) != 64 {
		t.Fatalf("payload digest: %q", first.PayloadDigest)
	}
}
