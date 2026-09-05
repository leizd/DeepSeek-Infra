package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
	modernsqlite "modernc.org/sqlite"
)

type failingScanner struct {
	err error
}

func (scanner failingScanner) Scan(_ ...any) error {
	return scanner.err
}

func TestControlOpenAndConnectionFailuresAreClosed(t *testing.T) {
	t.Run("parent is a file", func(t *testing.T) {
		parent := filepath.Join(t.TempDir(), "file")
		if err := os.WriteFile(parent, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := OpenControl(OpenOptions{Path: filepath.Join(parent, "child"), Owner: "owner-a"}); err == nil {
			t.Fatal("store below a file must fail")
		}
	})

	t.Run("database path is a directory", func(t *testing.T) {
		root := t.TempDir()
		if err := os.Mkdir(filepath.Join(root, ControlDatabaseFilename), 0o700); err != nil {
			t.Fatal(err)
		}
		if _, err := OpenControl(OpenOptions{Path: root, Owner: "owner-a"}); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("database directory: %v", err)
		}
	})

	t.Run("empty database file resumes initialization", func(t *testing.T) {
		root := t.TempDir()
		if err := os.WriteFile(filepath.Join(root, ControlDatabaseFilename), nil, 0o600); err != nil {
			t.Fatal(err)
		}
		control, err := OpenControl(OpenOptions{Path: root, Owner: "owner-a"})
		if err != nil {
			t.Fatal(err)
		}
		defer control.Close()
		if control.SchemaVersion() != SchemaV3 {
			t.Fatalf("schema version: %d", control.SchemaVersion())
		}
	})

	t.Run("empty sqlite header resumes initialization", func(t *testing.T) {
		root := t.TempDir()
		path := filepath.Join(root, ControlDatabaseFilename)
		connector, err := modernsqlite.NewConnector(controlDatabaseDSN(path))
		if err != nil {
			t.Fatal(err)
		}
		db := sql.OpenDB(connector)
		if err := db.Ping(); err != nil {
			t.Fatal(err)
		}
		if err := db.Close(); err != nil {
			t.Fatal(err)
		}
		info, err := os.Stat(path)
		if err != nil || info.Size() == 0 {
			t.Fatalf("empty sqlite header was not materialized: info=%v err=%v", info, err)
		}
		control, err := OpenControl(OpenOptions{Path: root, Owner: "owner-a"})
		if err != nil {
			t.Fatal(err)
		}
		defer control.Close()
		if control.SchemaVersion() != SchemaV3 {
			t.Fatalf("schema version: %d", control.SchemaVersion())
		}
	})

	t.Run("incomplete marker set", func(t *testing.T) {
		control := openShadow(t)
		path := control.path
		if _, err := control.db.Exec("DROP TABLE schema_migrations"); err != nil {
			t.Fatal(err)
		}
		if err := control.db.Close(); err != nil {
			t.Fatal(err)
		}
		control.closed = true
		if _, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b"}); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("incomplete marker set: %v", err)
		}
	})

	t.Run("closed connection", func(t *testing.T) {
		control := openShadow(t)
		if err := control.db.Close(); err != nil {
			t.Fatal(err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); err == nil {
			t.Fatal("put on a closed database must fail")
		}
		if _, _, err := control.Get("policy", "p1"); err == nil {
			t.Fatal("get on a closed database must fail")
		}
		if _, err := control.ExportSnapshot(); err == nil {
			t.Fatal("snapshot on a closed database must fail")
		}
		if err := control.Rollback(0); err == nil {
			t.Fatal("rollback on a closed database must fail")
		}
		if err := control.Close(); err == nil {
			t.Fatal("close must report failure to expire a lease on a closed database")
		}
	})
}

func TestControlMigrationAndSchemaCorruptionAreClosed(t *testing.T) {
	t.Run("inactive schema verification", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback()
		if err := verifySchemaTx(tx, 0); err != nil {
			t.Fatalf("schema zero: %v", err)
		}
	})

	t.Run("missing domain table", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TABLE forecasts"); err != nil {
			t.Fatal(err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("missing table: %v", err)
		}
	})

	t.Run("unexpected migration", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec(
			"INSERT INTO schema_migrations(version, applied_at, description) VALUES(4, 1, 'foreign')",
		); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ExportSnapshot(); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("foreign migration: %v", err)
		}
	})

	t.Run("invalid writer metadata", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("PRAGMA ignore_check_constraints = ON"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("UPDATE control_writer SET fencing_token = 0 WHERE singleton = 1"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.ExportSnapshot(); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("invalid writer: %v", err)
		}
	})

	t.Run("metadata row missing during verification", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DELETE FROM control_store_meta"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := verifySchemaTx(tx, SchemaV3); err == nil {
			t.Fatal("missing metadata row must fail verification")
		}
		_ = tx.Rollback()
	})

	t.Run("foreign metadata during verification", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("UPDATE control_store_meta SET unique_writer = 'python' WHERE singleton = 1"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := verifySchemaTx(tx, SchemaV3); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("foreign metadata: %v", err)
		}
		_ = tx.Rollback()
	})

	t.Run("writer table missing during verification", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TABLE control_writer"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := verifySchemaTx(tx, SchemaV3); err == nil {
			t.Fatal("missing writer table must fail verification")
		}
		_ = tx.Rollback()
	})

	t.Run("migration table missing during verification", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TABLE schema_migrations"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := verifySchemaTx(tx, SchemaV3); err == nil {
			t.Fatal("missing migration table must fail verification")
		}
		_ = tx.Rollback()
	})

	t.Run("unsupported in-memory migration", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		control.schema = SchemaV3 + 1
		if err := control.migrateTx(tx); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("future schema: %v", err)
		}
		_ = tx.Rollback()
		control.schema = SchemaV3
	})

	t.Run("conflicting migration object", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if err := control.Rollback(0); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("CREATE VIEW policies AS SELECT 'x' AS id"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := control.migrateTx(tx); err == nil {
			t.Fatal("conflicting migration object must fail")
		}
		_ = tx.Rollback()
	})

	t.Run("migration ledger conflict rolls back ddl", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if err := control.Rollback(0); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(
			"INSERT INTO schema_migrations(version, applied_at, description) VALUES(1, 1, 'conflict')",
		); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := control.migrateTx(tx); err == nil {
			t.Fatal("migration ledger conflict must fail")
		}
		_ = tx.Rollback()
		var count int
		if err := control.db.QueryRow(
			"SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table' AND name = 'policies'",
		).Scan(&count); err != nil || count != 0 {
			t.Fatalf("migration ddl escaped rollback: count=%d err=%v", count, err)
		}
	})
}

func TestBootstrapAndWriterMetadataFailuresAreClosed(t *testing.T) {
	t.Run("foreign metadata is rejected before writer activation", func(t *testing.T) {
		control := openShadow(t)
		path := control.path
		if _, err := control.db.Exec("UPDATE control_store_meta SET runtime = 'python' WHERE singleton = 1"); err != nil {
			t.Fatal(err)
		}
		if err := control.db.Close(); err != nil {
			t.Fatal(err)
		}
		control.closed = true
		if _, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b"}); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("foreign metadata: %v", err)
		}
	})

	t.Run("missing marker", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TABLE control_store_meta"); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(true); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("missing marker: %v", err)
		}
	})

	t.Run("empty existing metadata", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DELETE FROM control_store_meta"); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(true); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("empty existing metadata: %v", err)
		}
	})

	t.Run("multiple metadata rows", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("PRAGMA ignore_check_constraints = ON"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(
			"INSERT INTO control_store_meta(singleton, runtime, mode, schema_version, unique_writer) VALUES(2, 'go', 'shadow', 1, 'go')",
		); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(false); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("multiple metadata rows: %v", err)
		}
	})

	t.Run("malformed metadata schema", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TABLE control_store_meta"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("CREATE TABLE control_store_meta(singleton INTEGER PRIMARY KEY)"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("INSERT INTO control_store_meta(singleton) VALUES(1)"); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(false); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("malformed metadata: %v", err)
		}
	})

	t.Run("foreign in-memory metadata", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("UPDATE control_store_meta SET mode = 'authoritative' WHERE singleton = 1"); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(false); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("foreign metadata: %v", err)
		}
	})

	t.Run("live writer blocks bootstrap", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if err := control.bootstrapAndClaim(false); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("live writer: %v", err)
		}
	})

	t.Run("schema ledger mismatch", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("PRAGMA user_version = 0"); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(true); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("user_version mismatch: %v", err)
		}
	})

	t.Run("migration count mismatch", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec(
			"INSERT INTO schema_migrations(version, applied_at, description) VALUES(4, 1, 'foreign')",
		); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(true); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("migration count mismatch: %v", err)
		}
	})

	t.Run("unknown objects block writer claim", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("CREATE TABLE attacker_notes(id INTEGER PRIMARY KEY)"); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(true); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("unknown objects: %v", err)
		}
	})

	t.Run("bootstrap object collision", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TABLE control_store_meta"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("CREATE VIEW control_store_meta AS SELECT 1 AS singleton"); err != nil {
			t.Fatal(err)
		}
		if err := control.bootstrapAndClaim(false); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("bootstrap collision: %v", err)
		}
	})

	t.Run("invalid writer table", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TABLE control_writer"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("CREATE TABLE control_writer(singleton INTEGER PRIMARY KEY)"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := control.claimWriterTx(tx); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("invalid writer table: %v", err)
		}
		_ = tx.Rollback()
	})

	t.Run("exhausted writer token", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec(
			"UPDATE control_writer SET fencing_token = ?, lease_until = 0 WHERE singleton = 1",
			int64(math.MaxInt64),
		); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := control.claimWriterTx(tx); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("exhausted writer token: %v", err)
		}
		_ = tx.Rollback()
	})

	t.Run("writer insert failure", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DELETE FROM control_writer"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(`
			CREATE TRIGGER reject_writer_insert
			BEFORE INSERT ON control_writer
			BEGIN
				SELECT RAISE(ABORT, 'reject writer insert');
			END
		`); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := control.claimWriterTx(tx); err == nil || !strings.Contains(err.Error(), "reject writer insert") {
			t.Fatalf("writer insert failure: %v", err)
		}
		_ = tx.Rollback()
	})
}

func TestControlRecordAndHistoryCorruptionAreClosed(t *testing.T) {
	t.Run("scanner error", func(t *testing.T) {
		injected := errors.New("scan failure")
		if _, _, err := readControlRecord(failingScanner{err: injected}, "policy"); !errors.Is(err, injected) {
			t.Fatalf("scanner error: %v", err)
		}
	})

	t.Run("orphaned event", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		record := Record{
			Domain: "policy", ID: "orphan", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}
		digest, err := protocol.Digest(record)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(
			`INSERT INTO control_events(
				domain, record_id, revision, execution_epoch, state, payload_json,
				record_digest, writer_fencing_token, recorded_at
			) VALUES('policy', 'orphan', 1, 0, 'ACTIVE', '{}', ?, 1, 1)`,
			digest,
		); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.Get("policy", "orphan"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("orphaned event: %v", err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "orphan", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("orphaned event write: %v", err)
		}
	})

	t.Run("invalid canonical database payload", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		putPolicy(t, control, "p1")
		if _, err := control.db.Exec("PRAGMA ignore_check_constraints = ON"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("UPDATE policies SET payload_json = '{' WHERE id = 'p1'"); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.Get("policy", "p1"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("invalid payload: %v", err)
		}
	})

	t.Run("invalid scalar record metadata", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		putPolicy(t, control, "p1")
		if _, err := control.db.Exec("PRAGMA ignore_check_constraints = ON"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("UPDATE policies SET revision = 0 WHERE id = 'p1'"); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.Get("policy", "p1"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("invalid scalar metadata: %v", err)
		}
	})

	t.Run("unknown state with matching digest", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		putPolicy(t, control, "p1")
		record := Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "BOGUS", Payload: json.RawMessage(`{}`),
		}
		digest, err := protocol.Digest(record)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(
			"UPDATE policies SET state = 'BOGUS', record_digest = ? WHERE id = 'p1'",
			digest,
		); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.Get("policy", "p1"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("unknown state: %v", err)
		}
	})

	t.Run("latest corruption blocks writes and snapshots", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		putPolicy(t, control, "p1")
		if _, err := control.db.Exec(
			"UPDATE policies SET record_digest = ? WHERE id = 'p1'",
			strings.Repeat("0", 64),
		); err != nil {
			t.Fatal(err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 2, State: "DISABLED", Payload: json.RawMessage(`{}`),
		}); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt latest write: %v", err)
		}
		if _, err := control.ExportSnapshot(); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt latest snapshot: %v", err)
		}
	})

	t.Run("regressing event metadata", func(t *testing.T) {
		now := int64(10)
		control, err := OpenControl(OpenOptions{
			Path: t.TempDir(), Owner: "owner-a", LeaseSeconds: 30, Now: func() int64 { return now },
		})
		if err != nil {
			t.Fatal(err)
		}
		defer control.Close()
		putPolicy(t, control, "p1")
		now = 11
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 2, State: "DISABLED", Payload: json.RawMessage(`{}`),
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(
			"UPDATE control_events SET recorded_at = 9 WHERE domain = 'policy' AND record_id = 'p1' AND revision = 2",
		); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(controlEventImmutabilityTriggers[0]); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.Get("policy", "p1"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("regressing event metadata: %v", err)
		}
	})

	t.Run("corrupt event blocks writes and snapshots", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		putPolicy(t, control, "p1")
		if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(
			"UPDATE control_events SET record_digest = ? WHERE domain = 'policy' AND record_id = 'p1'",
			strings.Repeat("0", 64),
		); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(controlEventImmutabilityTriggers[0]); err != nil {
			t.Fatal(err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 2, State: "DISABLED", Payload: json.RawMessage(`{}`),
		}); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt event write: %v", err)
		}
		if _, err := control.ExportSnapshot(); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt event snapshot: %v", err)
		}
	})

	t.Run("invalid and stale event fences", func(t *testing.T) {
		zero := openShadow(t)
		defer zero.Close()
		if err := zero.Put(Record{
			Domain: "action", ID: "a1", Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: json.RawMessage(`{}`),
		}); err != nil {
			t.Fatal(err)
		}
		zeroRecord := Record{
			Domain: "action", ID: "a1", Revision: 1, ExecutionEpoch: 0, State: "PENDING", Payload: json.RawMessage(`{}`),
		}
		zeroDigest, _ := protocol.Digest(zeroRecord)
		if _, err := zero.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
			t.Fatal(err)
		}
		if _, err := zero.db.Exec(
			"UPDATE control_events SET execution_epoch = 0, record_digest = ? WHERE domain = 'action' AND record_id = 'a1'",
			zeroDigest,
		); err != nil {
			t.Fatal(err)
		}
		if _, err := zero.db.Exec(controlEventImmutabilityTriggers[0]); err != nil {
			t.Fatal(err)
		}
		if _, _, err := zero.Get("action", "a1"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("zero event fence: %v", err)
		}

		stale := openShadow(t)
		defer stale.Close()
		if err := stale.Put(Record{
			Domain: "action", ID: "a2", Revision: 1, ExecutionEpoch: 2, State: "PENDING", Payload: json.RawMessage(`{}`),
		}); err != nil {
			t.Fatal(err)
		}
		if err := stale.Put(Record{
			Domain: "action", ID: "a2", Revision: 2, ExecutionEpoch: 3, State: "CLAIMED", Payload: json.RawMessage(`{}`),
		}); err != nil {
			t.Fatal(err)
		}
		staleRecord := Record{
			Domain: "action", ID: "a2", Revision: 2, ExecutionEpoch: 1, State: "CLAIMED", Payload: json.RawMessage(`{}`),
		}
		staleDigest, _ := protocol.Digest(staleRecord)
		if _, err := stale.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
			t.Fatal(err)
		}
		if _, err := stale.db.Exec(
			"UPDATE control_events SET execution_epoch = 1, record_digest = ? WHERE domain = 'action' AND record_id = 'a2' AND revision = 2",
			staleDigest,
		); err != nil {
			t.Fatal(err)
		}
		if _, err := stale.db.Exec(controlEventImmutabilityTriggers[0]); err != nil {
			t.Fatal(err)
		}
		if _, _, err := stale.Get("action", "a2"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("stale event fence: %v", err)
		}
	})

	t.Run("history query and terminal mismatch", func(t *testing.T) {
		missingEvents := openShadow(t)
		defer missingEvents.Close()
		putPolicy(t, missingEvents, "p1")
		latest, _, err := missingEvents.Get("policy", "p1")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := missingEvents.db.Exec("DROP TABLE control_events"); err != nil {
			t.Fatal(err)
		}
		tx, err := missingEvents.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := validateControlHistory(tx, latest); err == nil {
			t.Fatal("missing event table must fail history query")
		}
		_ = tx.Rollback()

		mismatch := openShadow(t)
		defer mismatch.Close()
		putPolicy(t, mismatch, "p2")
		if err := mismatch.Put(Record{
			Domain: "policy", ID: "p2", Revision: 2, State: "DISABLED", Payload: json.RawMessage(`{}`),
		}); err != nil {
			t.Fatal(err)
		}
		third := Record{
			Domain: "policy", ID: "p2", Revision: 3, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}
		digest, _ := protocol.Digest(third)
		if _, err := mismatch.db.Exec(
			"UPDATE policies SET revision = 3, state = 'ACTIVE', record_digest = ? WHERE id = 'p2'",
			digest,
		); err != nil {
			t.Fatal(err)
		}
		if _, _, err := mismatch.Get("policy", "p2"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("terminal mismatch: %v", err)
		}
	})

	t.Run("latest metadata lookup failure", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		putPolicy(t, control, "p1")
		latest, _, err := control.Get("policy", "p1")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("DROP TABLE policies"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := validateControlHistory(tx, latest); err == nil {
			t.Fatal("missing latest table must fail metadata lookup")
		}
		_ = tx.Rollback()
	})

	t.Run("orphan query failure", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TABLE control_events"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := validateNoControlHistory(tx, "policy", "p1"); err == nil {
			t.Fatal("missing event table must fail orphan query")
		}
		_ = tx.Rollback()
	})

	t.Run("unknown history domain", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := validateControlHistory(tx, Record{}); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("unknown history domain: %v", err)
		}
		_ = tx.Rollback()
	})

	t.Run("canonical expansion exceeds limit", func(t *testing.T) {
		raw := json.RawMessage(`"` + strings.Repeat("<", maximumPayloadBytes-2) + `"`)
		if _, err := canonicalControlPayload(raw); !errors.Is(err, ErrInvalidPayload) {
			t.Fatalf("expanded payload: %v", err)
		}
	})
	if knownState("missing", "ACTIVE") {
		t.Fatal("unknown domain state")
	}
}

func TestControlWriterAndRollbackFailuresAreClosed(t *testing.T) {
	t.Run("negative and overflowing clock", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.nextLeaseUntil(-1); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("negative clock: %v", err)
		}
		lease, err := control.nextLeaseUntil(math.MaxInt64)
		if err != nil || lease != math.MaxInt64 {
			t.Fatalf("overflow clamp: %d %v", lease, err)
		}
		control.now = func() int64 { return -1 }
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("negative clock put: %v", err)
		}
	})

	t.Run("missing writer row", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DELETE FROM control_writer"); err != nil {
			t.Fatal(err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("missing writer: %v", err)
		}
	})

	t.Run("writer renewal rejected", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec(`
			CREATE TRIGGER reject_writer_renew
			BEFORE UPDATE ON control_writer
			BEGIN
				SELECT RAISE(ABORT, 'reject writer renew');
			END
		`); err != nil {
			t.Fatal(err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); err == nil || !strings.Contains(err.Error(), "reject writer renew") {
			t.Fatalf("writer renewal failure: %v", err)
		}
		if _, err := control.db.Exec("DROP TRIGGER reject_writer_renew"); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("writer renewal affects no row", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec(`
			CREATE TRIGGER ignore_writer_renew
			BEFORE UPDATE ON control_writer
			BEGIN
				SELECT RAISE(IGNORE);
			END
		`); err != nil {
			t.Fatal(err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("ignored writer renewal: %v", err)
		}
		if _, err := control.db.Exec("DROP TRIGGER ignore_writer_renew"); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("closed writer transaction", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		control.closed = true
		if _, err := control.assertWriterTx(tx, control.now()); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("closed writer transaction: %v", err)
		}
		control.closed = false
		_ = tx.Rollback()
	})

	t.Run("insert and event failures roll back", func(t *testing.T) {
		insertFailure := openShadow(t)
		defer insertFailure.Close()
		if _, err := insertFailure.db.Exec(`
			CREATE TRIGGER reject_policy_insert
			BEFORE INSERT ON policies
			BEGIN
				SELECT RAISE(ABORT, 'reject policy insert');
			END
		`); err != nil {
			t.Fatal(err)
		}
		if err := insertFailure.Put(Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); err == nil || !strings.Contains(err.Error(), "reject policy insert") {
			t.Fatalf("policy insert failure: %v", err)
		}

		eventFailure := openShadow(t)
		defer eventFailure.Close()
		if _, err := eventFailure.db.Exec(`
			CREATE TRIGGER reject_event_insert
			BEFORE INSERT ON control_events
			BEGIN
				SELECT RAISE(ABORT, 'reject event insert');
			END
		`); err != nil {
			t.Fatal(err)
		}
		if err := eventFailure.Put(Record{
			Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
		}); err == nil || !strings.Contains(err.Error(), "reject event insert") {
			t.Fatalf("event insert failure: %v", err)
		}
		var count int
		if err := eventFailure.db.QueryRow("SELECT COUNT(*) FROM policies").Scan(&count); err != nil || count != 0 {
			t.Fatalf("event failure did not roll back record: count=%d err=%v", count, err)
		}
	})

	t.Run("ignored record update is a revision conflict", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		putPolicy(t, control, "p1")
		if _, err := control.db.Exec(`
			CREATE TRIGGER ignore_policy_update
			BEFORE UPDATE ON policies
			BEGIN
				SELECT RAISE(IGNORE);
			END
		`); err != nil {
			t.Fatal(err)
		}
		if err := control.Put(Record{
			Domain: "policy", ID: "p1", Revision: 2, State: "DISABLED", Payload: json.RawMessage(`{}`),
		}); !errors.Is(err, ErrRevisionConflict) {
			t.Fatalf("ignored record update: %v", err)
		}
	})

	t.Run("rollback object and ledger failures", func(t *testing.T) {
		objectFailure := openShadow(t)
		defer objectFailure.Close()
		if _, err := objectFailure.db.Exec("DROP TABLE control_events"); err != nil {
			t.Fatal(err)
		}
		if _, err := objectFailure.db.Exec("CREATE VIEW control_events AS SELECT 1 AS event_id"); err != nil {
			t.Fatal(err)
		}
		if err := objectFailure.Rollback(0); err == nil {
			t.Fatal("rollback must reject a conflicting event view")
		}

		ledgerFailure := openShadow(t)
		defer ledgerFailure.Close()
		if _, err := ledgerFailure.db.Exec(`
			CREATE TRIGGER reject_migration_delete
			BEFORE DELETE ON schema_migrations
			BEGIN
				SELECT RAISE(ABORT, 'reject migration delete');
			END
		`); err != nil {
			t.Fatal(err)
		}
		if err := ledgerFailure.Rollback(0); err == nil {
			t.Fatal("rollback must surface migration ledger failure")
		}
		var count int
		if err := ledgerFailure.db.QueryRow(
			"SELECT COUNT(*) FROM sqlite_schema WHERE type = 'table' AND name = 'policies'",
		).Scan(&count); err != nil || count != 1 {
			t.Fatalf("rollback escaped transaction: count=%d err=%v", count, err)
		}

		domainFailure := openShadow(t)
		defer domainFailure.Close()
		if _, err := domainFailure.db.Exec("DROP TABLE policies"); err != nil {
			t.Fatal(err)
		}
		if _, err := domainFailure.db.Exec("CREATE VIEW policies AS SELECT 'x' AS id"); err != nil {
			t.Fatal(err)
		}
		if err := domainFailure.Rollback(0); err == nil {
			t.Fatal("rollback must reject a conflicting domain view")
		}

		metaFailure := openShadow(t)
		defer metaFailure.Close()
		if _, err := metaFailure.db.Exec(`
			CREATE TRIGGER reject_meta_update
			BEFORE UPDATE ON control_store_meta
			BEGIN
				SELECT RAISE(ABORT, 'reject meta update');
			END
		`); err != nil {
			t.Fatal(err)
		}
		if err := metaFailure.Rollback(0); err == nil || !strings.Contains(err.Error(), "reject meta update") {
			t.Fatalf("rollback metadata failure: %v", err)
		}
	})
}

func putPolicy(t *testing.T, control *Control, id string) {
	t.Helper()
	if err := control.Put(Record{
		Domain: "policy", ID: id, Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}
}
