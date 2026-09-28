package store

import (
	"errors"
	"testing"
	"time"
)

// The v8 shape froze `result_status = 'PROPOSED'`. This rebuilds a store in that
// shape around a real operation row so the upgrade can be proven to preserve the
// journal — losing an operation row would let a replayed request apply twice.
func prepareOperationV8Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, statement := range []string{
		"DROP TRIGGER control_operations_no_update",
		"DROP TRIGGER control_operations_no_delete",
		"ALTER TABLE control_operations RENAME TO control_operations_v9_fixture",
		`CREATE TABLE control_operations (
			operation_id TEXT PRIMARY KEY CHECK(length(operation_id) = 64 AND operation_id = lower(operation_id)),
			request_id TEXT NOT NULL UNIQUE CHECK(length(request_id) = 64 AND request_id = lower(request_id)),
			nonce TEXT NOT NULL UNIQUE CHECK(length(nonce) = 64 AND nonce = lower(nonce)),
			domain TEXT NOT NULL CHECK(domain IN (
				'policy', 'target', 'scheduler_run', 'action', 'risk', 'wave',
				'peer', 'grant', 'session', 'transfer', 'forecast', 'agent_run'
			)),
			action_id TEXT NOT NULL CHECK(length(action_id) > 0),
			execution_epoch INTEGER NOT NULL CHECK(execution_epoch > 0),
			fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
			payload_digest TEXT NOT NULL CHECK(length(payload_digest) > 0),
			request_digest TEXT NOT NULL CHECK(length(request_digest) > 0),
			canonical_request TEXT NOT NULL CHECK(json_valid(canonical_request) AND length(canonical_request) > 0),
			result_status TEXT NOT NULL CHECK(result_status = 'PROPOSED'),
			result_json TEXT NOT NULL CHECK(json_valid(result_json)),
			writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token > 0),
			recorded_at INTEGER NOT NULL CHECK(recorded_at >= 0)
		) STRICT`,
		`INSERT INTO control_operations(` + controlOperationsV9Columns + `)
		 SELECT ` + controlOperationsV9Columns + ` FROM control_operations_v9_fixture`,
		"DROP TABLE control_operations_v9_fixture",
		controlOperationImmutabilityTriggers[0],
		controlOperationImmutabilityTriggers[1],
		`CREATE TABLE control_meta_v8_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 8),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v8_fixture SELECT singleton,runtime,mode,8,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta", "ALTER TABLE control_meta_v8_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=9", "PRAGMA user_version=8",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV8); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV8
}

func TestOperationStatusV8UpgradePreservesTheJournal(t *testing.T) {
	control := openControlAt(t, 1000)
	private, public := rfc8032MutationKeys(t)
	now := time.Unix(1000, 0).UTC()
	raw := signPolicyMutation(t, control, private, public, hex64(0x11), hex64(0x22), hex64(0x33), now, nil)
	accepted, err := control.AcceptMutation(raw, mutationAuth(public, now))
	if err != nil {
		t.Fatal(err)
	}
	path := control.path
	prepareOperationV8Fixture(t, control)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}

	// A migration that fails must roll back and leave the store readable at v8.
	calls := 0
	failed, err := OpenControl(OpenOptions{Path: path, Owner: "failed", Now: func() int64 {
		calls++
		if calls == 1 {
			return 1001
		}
		return -1
	}})
	if !errors.Is(err, ErrWriterFenceHeld) {
		if failed != nil {
			_ = failed.Close()
		}
		t.Fatalf("v9 migration did not roll back: %v", err)
	}

	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "successor", Now: func() int64 { return 1011 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("schema after upgrade: %d", reopened.SchemaVersion())
	}
	// The journal survived, and it still answers the replay question: the same
	// operation is reported as an idempotent no-op rather than applied again.
	preserved, exists, err := reopened.GetOperation(accepted.OperationID)
	if err != nil || !exists || preserved.OperationID != accepted.OperationID || preserved.Status != MutationProposed {
		t.Fatalf("operation journal after upgrade: %+v exists=%v %v", preserved, exists, err)
	}
	replay, err := reopened.AcceptMutation(raw, mutationAuth(public, time.Unix(1011, 0).UTC()))
	if err != nil || replay.Status != MutationAlreadyApplied {
		t.Fatalf("replay after upgrade: %+v %v", replay, err)
	}
	// And the widened journal can now record an applied result.
	if _, err := reopened.db.Exec(`INSERT INTO control_operations(
		operation_id, request_id, nonce, domain, action_id, execution_epoch, fencing_token,
		payload_digest, request_digest, canonical_request, result_status, result_json,
		writer_fencing_token, recorded_at
	) VALUES(?, ?, ?, 'policy', 'pol-1', 1, 1, 'd', 'r', '{}', 'APPLIED', '{}', 1, 1)`,
		hex64(0x44), hex64(0x55), hex64(0x66),
	); err != nil {
		t.Fatalf("v9 journal must admit an applied result: %v", err)
	}
}

// A store whose journal still cannot record an applied result must be refused at
// open, not served with a journal that misreports an applied mutation.
func TestOperationStatusSchemaIsRequired(t *testing.T) {
	control := openControlAt(t, 1000)
	if _, err := control.db.Exec("DROP TRIGGER control_operations_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_operations_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("ALTER TABLE control_operations RENAME TO control_operations_legacy"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`CREATE TABLE control_operations (
		operation_id TEXT PRIMARY KEY,
		request_id TEXT NOT NULL UNIQUE,
		nonce TEXT NOT NULL UNIQUE,
		domain TEXT NOT NULL,
		action_id TEXT NOT NULL,
		execution_epoch INTEGER NOT NULL,
		fencing_token INTEGER NOT NULL,
		payload_digest TEXT NOT NULL,
		request_digest TEXT NOT NULL,
		canonical_request TEXT NOT NULL,
		result_status TEXT NOT NULL CHECK(result_status = 'PROPOSED'),
		result_json TEXT NOT NULL,
		writer_fencing_token INTEGER NOT NULL,
		recorded_at INTEGER NOT NULL
	) STRICT`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlOperationImmutabilityTriggers[0]); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlOperationImmutabilityTriggers[1]); err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	if _, err := control.ExportSnapshot(); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("proposed-only journal accepted: %v", err)
	}
	if err := control.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE"}); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("proposed-only journal served a write: %v", err)
	}
}
