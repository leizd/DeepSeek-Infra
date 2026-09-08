package store

import (
	"crypto/ed25519"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	modernsqlite "modernc.org/sqlite"
)

func TestAcceptMutationJournalsProposedWithoutProductionApply(t *testing.T) {
	now := mutationNow()
	store := openControlAt(t, now.Unix())
	defer store.Close()
	private, public := rfc8032MutationKeys(t)
	raw := signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil)

	got, err := store.AcceptMutation(raw, mutationAuth(public, now))
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != MutationProposed || got.OperationID != hex64(0x33) || got.RequestID != hex64(0x11) || got.Domain != "policy" {
		t.Fatalf("proposed: %+v", got)
	}
	if got.PayloadDigest == "" || got.RequestDigest == "" {
		t.Fatalf("digests: %+v", got)
	}
	stored, ok, err := store.GetOperation(got.OperationID)
	if err != nil || !ok || stored.Status != MutationProposed || stored.OperationID != got.OperationID ||
		stored.PayloadDigest != got.PayloadDigest {
		t.Fatalf("stored: %+v ok=%v err=%v", stored, ok, err)
	}
	if policyRowCount(t, store) != 0 {
		t.Fatal("AcceptMutation must not persist a policy record")
	}
	if err := store.MutateProduction("policy", map[string]any{"id": "p1"}); err != internalprotocol.ErrMutationDenied {
		t.Fatalf("production mutation: %v", err)
	}

	retry, err := store.AcceptMutation(raw, mutationAuth(public, now))
	if err != nil {
		t.Fatal(err)
	}
	if retry.Status != MutationAlreadyApplied || retry.OperationID != got.OperationID || retry.PayloadDigest != got.PayloadDigest {
		t.Fatalf("retry: %+v", retry)
	}
	if policyRowCount(t, store) != 0 {
		t.Fatal("idempotent retry must not persist a policy record")
	}
}

func TestAcceptMutationReplayAndNonceFailClosed(t *testing.T) {
	now := mutationNow()
	store := openControlAt(t, now.Unix())
	defer store.Close()
	private, public := rfc8032MutationKeys(t)
	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil), mutationAuth(public, now)); err != nil {
		t.Fatal(err)
	}

	conflict, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x44), hex64(0x55), hex64(0x33), now, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["state"] = "DISABLED"
	}), mutationAuth(public, now))
	if !errors.Is(err, ErrMutationRequestReplayConflict) || conflict.Status != "" {
		t.Fatalf("payload conflict: %+v %v", conflict, err)
	}

	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x66), hex64(0x77), now, nil), mutationAuth(public, now)); !errors.Is(err, ErrMutationRequestReplay) {
		t.Fatalf("request replay: %v", err)
	}
	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x88), hex64(0x22), hex64(0x99), now, nil), mutationAuth(public, now)); !errors.Is(err, ErrMutationRequestNonceReuse) {
		t.Fatalf("nonce reuse: %v", err)
	}
	if policyRowCount(t, store) != 0 {
		t.Fatal("conflict paths must not persist a policy record")
	}
}

func TestAcceptMutationFenceEpochAndEnvelopeFailClosed(t *testing.T) {
	now := mutationNow()
	store := openControlAt(t, now.Unix())
	defer store.Close()
	private, public := rfc8032MutationKeys(t)
	auth := mutationAuth(public, now)

	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, func(document map[string]any) {
		document["fencingToken"] = json.Number("9")
	}), auth); !errors.Is(err, ErrMutationRequestStaleFencingToken) {
		t.Fatalf("stale fence: %v", err)
	}
	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, func(document map[string]any) {
		document["executionEpoch"] = json.Number("1")
	}), auth); !errors.Is(err, internalprotocol.ErrStaleEpoch) {
		t.Fatalf("stale epoch: %v", err)
	}
	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, func(document map[string]any) {
		document["revision"] = json.Number("9")
	}), auth); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("revision: %v", err)
	}

	fixture := loadMutationRequestFixture(t)
	if _, err := store.AcceptMutation([]byte(fixture.CanonicalRequest), MutationAuthority{
		SignerPublicKey: fixture.SignerPublicKey,
		FleetID:         "fleet-a",
		Environment:     "test",
		Now:             now,
	}); !errors.Is(err, ErrMutationRequestStaleFencingToken) {
		t.Fatalf("frozen vector: %v", err)
	}
	if _, err := store.AcceptMutation([]byte(`{"domain":"nope"}`), auth); !errors.Is(err, ErrUnknownDomain) {
		t.Fatalf("unknown domain: %v", err)
	}
	if _, err := store.AcceptMutation([]byte("null"), auth); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("null: %v", err)
	}
	if _, err := store.AcceptMutation([]byte("{"), auth); !errors.Is(err, ErrMutationRequestInvalid) {
		t.Fatalf("truncated: %v", err)
	}
	if _, err := store.AcceptMutation([]byte(`{"domain":"policy"}`), auth); !errors.Is(err, ErrMutationRequestFieldsInvalid) && !errors.Is(err, ErrMutationRequestCanonicalMismatch) {
		t.Fatalf("incomplete: %v", err)
	}
	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil), MutationAuthority{
		SignerPublicKey: "not-a-key",
		FleetID:         "fleet-a",
		Environment:     "test",
		Now:             now,
	}); !errors.Is(err, ErrMutationRequestSignerMismatch) {
		t.Fatalf("signer: %v", err)
	}
	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil), MutationAuthority{
		SignerPublicKey: public,
		FleetID:         "fleet-b",
		Environment:     "test",
		Now:             now,
	}); !errors.Is(err, ErrMutationRequestFleetMismatch) {
		t.Fatalf("fleet: %v", err)
	}
	if _, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0xa1), hex64(0xa2), hex64(0xa3), now, func(document map[string]any) {
		payload := document["payload"].(map[string]any)
		payload["password"] = "secret"
	}), auth); !errors.Is(err, ErrMutationRequestSecretDetected) {
		t.Fatalf("secret: %v", err)
	}
	if operationRowCount(t, store) != 0 || policyRowCount(t, store) != 0 {
		t.Fatal("fail-closed accept must not journal or mutate policy")
	}
}

func TestAcceptMutationDualEvaluateStillJournalsOnly(t *testing.T) {
	now := mutationNow()
	store := openControlAt(t, now.Unix())
	defer store.Close()
	current, err := store.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	dual, err := store.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverDualEvaluate,
		ExpectedRevision: current.Revision,
		ExpectedEpoch:    current.Epoch,
		FencingToken:     current.FencingToken,
		TransferID:       "policy-dual-journal",
	})
	if err != nil {
		t.Fatal(err)
	}
	private, public := rfc8032MutationKeys(t)
	got, err := store.AcceptMutation(signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil), mutationAuth(public, now))
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != MutationProposed || got.Domain != "policy" {
		t.Fatalf("dual evaluate journal: %+v", got)
	}
	if policyRowCount(t, store) != 0 {
		t.Fatal("dual-evaluate AcceptMutation must not persist a policy record")
	}
	if err := store.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatalf("shadow put during dual-evaluate: %v", err)
	}
	if err := store.MutateProduction("policy", map[string]any{"id": "p1"}); err != internalprotocol.ErrMutationDenied {
		t.Fatalf("production mutation: %v", err)
	}
	if dual.State != CutoverDualEvaluate {
		t.Fatalf("cutover: %+v", dual)
	}
}

func TestAcceptMutationGoAuthoritativeRemainsUnauthorized(t *testing.T) {
	now := mutationNow()
	store := openControlAt(t, now.Unix())
	defer store.Close()
	private, public := rfc8032MutationKeys(t)
	raw := signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil)
	if _, err := store.db.Exec("UPDATE control_cutover SET state = 'go_authoritative', owner = 'go' WHERE domain = 'policy'"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcceptMutation(raw, mutationAuth(public, now)); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("go authoritative accept: %v", err)
	}
	if operationRowCount(t, store) != 0 {
		t.Fatal("unauthorized cutover must not journal")
	}
	if err := store.MutateProduction("policy", map[string]any{"id": "p1"}); err != internalprotocol.ErrMutationDenied {
		t.Fatalf("production mutation: %v", err)
	}
}

func TestControlOperationsAreImmutableAndInsertFailureRollsBack(t *testing.T) {
	now := mutationNow()
	store := openControlAt(t, now.Unix())
	defer store.Close()
	private, public := rfc8032MutationKeys(t)
	raw := signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil)
	if _, err := store.db.Exec(`
		CREATE TRIGGER reject_operation
		BEFORE INSERT ON control_operations
		BEGIN
			SELECT RAISE(ABORT, 'injected operation failure');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcceptMutation(raw, mutationAuth(public, now)); err == nil {
		t.Fatal("insert failure must abort accept")
	}
	if _, ok, err := store.GetOperation(hex64(0x33)); err != nil || ok {
		t.Fatalf("partial journal escaped rollback: ok=%v err=%v", ok, err)
	}
	if _, err := store.db.Exec("DROP TRIGGER reject_operation"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcceptMutation(raw, mutationAuth(public, now)); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec("UPDATE control_operations SET result_status = 'PROPOSED' WHERE operation_id = ?", hex64(0x33)); err == nil {
		t.Fatal("operations must be immutable")
	}
	if _, err := store.db.Exec("DELETE FROM control_operations WHERE operation_id = ?", hex64(0x33)); err == nil {
		t.Fatal("operations must not be deleted")
	}
}

func TestGetOperationAndAcceptMutationRequireLiveSchemaAndWriter(t *testing.T) {
	now := mutationNow()
	store := openControlAt(t, now.Unix())
	private, public := rfc8032MutationKeys(t)
	raw := signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil)
	if _, ok, err := store.GetOperation("not-a-digest"); err != nil || ok {
		t.Fatalf("invalid id: ok=%v err=%v", ok, err)
	}
	if _, ok, err := store.GetOperation(hex64(0x33)); err != nil || ok {
		t.Fatalf("missing: ok=%v err=%v", ok, err)
	}
	if _, err := store.AcceptMutation(raw, MutationAuthority{
		SignerPublicKey: public,
		FleetID:         "fleet-a",
		Environment:     "test",
	}); err != nil {
		t.Fatalf("zero auth clock: %v", err)
	}
	if err := store.Rollback(0); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcceptMutation(raw, mutationAuth(public, now)); err != ErrSchemaInactive {
		t.Fatalf("inactive accept: %v", err)
	}
	if _, _, err := store.GetOperation(hex64(0x33)); err != ErrSchemaInactive {
		t.Fatalf("inactive get: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcceptMutation(raw, mutationAuth(public, now)); err != ErrWriterFenceHeld {
		t.Fatalf("closed accept: %v", err)
	}
	if _, _, err := store.GetOperation(hex64(0x33)); err != ErrWriterFenceHeld {
		t.Fatalf("closed get: %v", err)
	}

	closedDB := openControlAt(t, now.Unix())
	if err := closedDB.db.Close(); err != nil {
		t.Fatal(err)
	}
	closedDB.closed = false
	if _, err := closedDB.AcceptMutation(raw, mutationAuth(public, now)); err == nil {
		t.Fatal("closed db accept")
	}
	if _, _, err := closedDB.GetOperation(hex64(0x33)); err == nil {
		t.Fatal("closed db get")
	}

	path := t.TempDir()
	leaseNow := int64(1_000)
	first, err := OpenControl(OpenOptions{Path: path, Owner: "owner-a", Now: func() int64 { return leaseNow }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	issued := time.Unix(leaseNow, 0).UTC()
	staleRaw := signPolicyMutation(t, first, private, public, hex64(0x11), hex64(0x22), hex64(0x33), issued, nil)
	leaseNow = 1_011
	second, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b", Now: func() int64 { return leaseNow }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	if _, err := first.AcceptMutation(staleRaw, mutationAuth(public, issued)); err != ErrWriterFenceHeld {
		t.Fatalf("stale writer: %v", err)
	}
	_ = first.Close()
}

func TestControlMigratesFromV2ToOperationJournal(t *testing.T) {
	now := mutationNow()
	store := openControlAt(t, now.Unix())
	path := store.path
	databasePath := store.DatabasePath()
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	connector, err := modernsqlite.NewConnector(controlDatabaseDSN(databasePath))
	if err != nil {
		t.Fatal(err)
	}
	db := sql.OpenDB(connector)
	for _, statement := range []string{
		"DROP TABLE storage_dispatches",
		"DROP TABLE control_operations",
		"DELETE FROM schema_migrations WHERE version IN (3, 4)",
		"UPDATE control_store_meta SET schema_version = 2 WHERE singleton = 1",
		"PRAGMA user_version = 2",
	} {
		if _, err := db.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b", Now: func() int64 { return now.Unix() }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("migrated schema: %d", reopened.SchemaVersion())
	}
	private, public := rfc8032MutationKeys(t)
	got, err := reopened.AcceptMutation(signPolicyMutation(t, reopened, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil), mutationAuth(public, now))
	if err != nil || got.Status != MutationProposed {
		t.Fatalf("migrated accept: %+v %v", got, err)
	}
}

func prepareV3MigrationReplay(t *testing.T, tx *sql.Tx) {
	t.Helper()
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=3 WHERE singleton=1"); err != nil {
		t.Fatal(err)
	}
}

func TestMigrateToV3FailsClosedWithoutPartialJournal(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	tx, err := store.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	prepareV3MigrationReplay(t, tx)
	if err := store.migrateToV3Tx(tx); err == nil {
		t.Fatal("rebuilding an existing v3 operation schema must fail")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	if _, err := store.db.Exec("CREATE TABLE control_store_meta_v3(singleton INTEGER PRIMARY KEY) STRICT"); err != nil {
		t.Fatal(err)
	}
	tx, err = store.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := store.migrateToV3Tx(tx); err == nil {
		t.Fatal("existing metadata v3 table must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	negative := openShadow(t)
	defer negative.Close()
	negative.now = func() int64 { return -1 }
	tx, err = negative.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	prepareV3MigrationReplay(t, tx)
	if _, err := tx.Exec("DROP TABLE control_operations"); err != nil {
		t.Fatal(err)
	}
	if err := negative.migrateToV3Tx(tx); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("negative clock: %v", err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, ok, err := negative.GetOperation(hex64(0x33)); err != nil || ok {
		t.Fatalf("negative clock must not commit: ok=%v err=%v", ok, err)
	}

	copyFail := openShadow(t)
	defer copyFail.Close()
	tx, err = copyFail.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("DROP TABLE control_store_meta"); err != nil {
		t.Fatal(err)
	}
	if err := copyFail.migrateToV3Tx(tx); err == nil {
		t.Fatal("missing metadata copy source must fail v3 migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	eventsView := openShadow(t)
	defer eventsView.Close()
	tx, err = eventsView.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	prepareV3MigrationReplay(t, tx)
	if _, err := tx.Exec("DROP TABLE control_operations"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("CREATE VIEW control_operations AS SELECT 1 AS operation_id"); err != nil {
		t.Fatal(err)
	}
	if err := eventsView.migrateToV3Tx(tx); err == nil {
		t.Fatal("operation view must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	dupTrigger := openShadow(t)
	defer dupTrigger.Close()
	tx, err = dupTrigger.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	prepareV3MigrationReplay(t, tx)
	if _, err := tx.Exec("DROP TABLE control_operations"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`
		CREATE TRIGGER control_operations_no_update
		BEFORE UPDATE ON control_writer
		BEGIN
			SELECT RAISE(ABORT, 'reserved trigger name');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if err := dupTrigger.migrateToV3Tx(tx); err == nil {
		t.Fatal("duplicate operation trigger must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	dupMigration := openShadow(t)
	defer dupMigration.Close()
	tx, err = dupMigration.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	prepareV3MigrationReplay(t, tx)
	if _, err := tx.Exec("DROP TABLE control_operations"); err != nil {
		t.Fatal(err)
	}
	if err := dupMigration.migrateToV3Tx(tx); err == nil {
		t.Fatal("duplicate schema v3 ledger row must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	dropFail := openShadow(t)
	defer dropFail.Close()
	tx, err = dropFail.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	prepareV3MigrationReplay(t, tx)
	if _, err := tx.Exec(`CREATE TABLE operation_meta_ref (
		singleton INTEGER PRIMARY KEY REFERENCES control_store_meta(singleton)
	) STRICT`); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("INSERT INTO operation_meta_ref(singleton) VALUES(1)"); err != nil {
		t.Fatal(err)
	}
	if err := dropFail.migrateToV3Tx(tx); err == nil {
		t.Fatal("foreign key child must block metadata rebuild")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
}

func TestOperationHelpersAndCorruptJournalFailClosed(t *testing.T) {
	now := mutationNow()
	private, public := rfc8032MutationKeys(t)
	store := openShadow(t)
	defer store.Close()
	raw := signPolicyMutation(t, store, private, public, hex64(0x11), hex64(0x22), hex64(0x33), time.Unix(store.now(), 0).UTC(), nil)
	if _, err := store.db.Exec("DROP TRIGGER control_operations_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.AcceptMutation(raw, mutationAuth(public, now)); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing operation trigger accept: %v", err)
	}
	if _, _, err := store.GetOperation(hex64(0x33)); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing operation trigger: %v", err)
	}

	corrupt := openControlAt(t, now.Unix())
	defer corrupt.Close()
	corruptRaw := signPolicyMutation(t, corrupt, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil)
	if _, err := corrupt.db.Exec("PRAGMA ignore_check_constraints = ON"); err != nil {
		t.Fatal(err)
	}
	if _, err := corrupt.db.Exec("UPDATE control_cutover SET owner = 'rust' WHERE domain = 'policy'"); err != nil {
		t.Fatal(err)
	}
	if _, err := corrupt.AcceptMutation(corruptRaw, mutationAuth(public, now)); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt cutover accept: %v", err)
	}

	narrow := openShadow(t)
	defer narrow.Close()
	if _, err := narrow.db.Exec("DROP TABLE control_operations"); err != nil {
		t.Fatal(err)
	}
	if _, err := narrow.db.Exec("CREATE TABLE control_operations(operation_id TEXT PRIMARY KEY) STRICT"); err != nil {
		t.Fatal(err)
	}
	if _, err := narrow.db.Exec(`
		CREATE TRIGGER control_operations_no_update
		BEFORE UPDATE ON control_operations
		BEGIN
			SELECT RAISE(ABORT, 'CONTROL_OPERATION_IMMUTABLE');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := narrow.db.Exec(`
		CREATE TRIGGER control_operations_no_delete
		BEFORE DELETE ON control_operations
		BEGIN
			SELECT RAISE(ABORT, 'CONTROL_OPERATION_IMMUTABLE');
		END
	`); err != nil {
		t.Fatal(err)
	}
	now = time.Unix(narrow.now(), 0).UTC()
	if _, err := narrow.AcceptMutation(signPolicyMutation(t, narrow, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil), mutationAuth(public, now)); err == nil {
		t.Fatal("narrow operations table must fail closed")
	}
	if _, _, err := narrow.GetOperation(hex64(0x33)); err == nil {
		t.Fatal("narrow operations lookup must fail closed")
	}

	missing := openShadow(t)
	defer missing.Close()
	if _, err := missing.db.Exec("DROP TABLE control_operations"); err != nil {
		t.Fatal(err)
	}
	if err := missing.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing operations table: %v", err)
	}

	nullable := openControlAt(t, now.Unix())
	defer nullable.Close()
	if _, err := nullable.db.Exec("DROP TABLE control_operations"); err != nil {
		t.Fatal(err)
	}
	if _, err := nullable.db.Exec(`CREATE TABLE control_operations(
		operation_id TEXT,
		request_id TEXT,
		nonce TEXT,
		domain TEXT,
		payload_digest TEXT,
		request_digest TEXT
	)`); err != nil {
		t.Fatal(err)
	}
	if _, err := nullable.db.Exec(`
		CREATE TRIGGER control_operations_no_update
		BEFORE UPDATE ON control_operations
		BEGIN
			SELECT RAISE(ABORT, 'CONTROL_OPERATION_IMMUTABLE');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := nullable.db.Exec(`
		CREATE TRIGGER control_operations_no_delete
		BEFORE DELETE ON control_operations
		BEGIN
			SELECT RAISE(ABORT, 'CONTROL_OPERATION_IMMUTABLE');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := nullable.db.Exec("INSERT INTO control_operations(operation_id) VALUES(?)", hex64(0x33)); err != nil {
		t.Fatal(err)
	}
	if _, err := nullable.AcceptMutation(signPolicyMutation(t, nullable, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil), mutationAuth(public, now)); err == nil {
		t.Fatal("null operation row must fail closed")
	}
}

func mutationNow() time.Time {
	return time.Date(2026, 9, 5, 0, 0, 30, 0, time.UTC)
}

func openControlAt(t *testing.T, now int64) *Control {
	t.Helper()
	store, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "owner-a", Now: func() int64 { return now }, LeaseSeconds: 30})
	if err != nil {
		t.Fatal(err)
	}
	return store
}

func rfc8032MutationKeys(t *testing.T) (ed25519.PrivateKey, string) {
	t.Helper()
	seed, err := hex.DecodeString("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
	if err != nil {
		t.Fatal(err)
	}
	private := ed25519.NewKeyFromSeed(seed)
	return private, base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
}

func mutationAuth(public string, now time.Time) MutationAuthority {
	return MutationAuthority{
		SignerPublicKey: public,
		FleetID:         "fleet-a",
		Environment:     "test",
		Now:             now,
	}
}

func hex64(prefix byte) string {
	return strings.Repeat(fmt.Sprintf("%02x", prefix), 32)
}

func signPolicyMutation(t *testing.T, store *Control, private ed25519.PrivateKey, public, requestID, nonce, operationID string, now time.Time, override func(map[string]any)) []byte {
	t.Helper()
	cutover, err := store.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	unsigned := map[string]any{
		"schema":         MutationRequestSchema,
		"schemaVersion":  json.Number("1"),
		"domain":         "policy",
		"operation":      MutationOperationPropose,
		"actionId":       "pol-1",
		"executionEpoch": json.Number(fmt.Sprintf("%d", cutover.Epoch+1)),
		"fencingToken":   json.Number(fmt.Sprintf("%d", cutover.FencingToken)),
		"revision":       json.Number(fmt.Sprintf("%d", cutover.Revision)),
		"requestId":      requestID,
		"nonce":          nonce,
		"operationId":    operationID,
		"issuedAt":       now.UTC().Format(time.RFC3339),
		"expiresAt":      now.UTC().Add(300 * time.Second).Format(time.RFC3339),
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
	if override != nil {
		override(unsigned)
	}
	_, raw, err := SignMutationRequest(unsigned, private, public)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func policyRowCount(t *testing.T, store *Control) int {
	t.Helper()
	var count int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM policies").Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func operationRowCount(t *testing.T, store *Control) int {
	t.Helper()
	var count int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM control_operations").Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}
