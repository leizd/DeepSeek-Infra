package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestControlUsesOneHardenedSQLiteDatabase(t *testing.T) {
	control := openShadow(t)
	defer control.Close()

	databasePath := control.DatabasePath()
	if filepath.Base(databasePath) != ControlDatabaseFilename {
		t.Fatalf("database path: %q", databasePath)
	}
	if info, err := os.Stat(databasePath); err != nil || info.IsDir() {
		t.Fatalf("database file: info=%v err=%v", info, err)
	}
	for _, legacy := range []string{"manifest.json", "writer.json"} {
		if _, err := os.Stat(filepath.Join(control.path, legacy)); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("legacy file %q must not exist: %v", legacy, err)
		}
	}

	rows, err := control.db.Query("SELECT name FROM sqlite_schema WHERE type = 'table'")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	tables := map[string]bool{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			t.Fatal(err)
		}
		tables[name] = true
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	for _, table := range append([]string{"control_store_meta", "control_writer", "schema_migrations", "control_events", "control_cutover", "control_cutover_events", "control_operations"}, TableNames...) {
		if !tables[table] {
			t.Fatalf("missing SQL table %q: %v", table, tables)
		}
	}
	var userVersion, migrationCount, cutoverCount int
	if err := control.db.QueryRow("PRAGMA user_version").Scan(&userVersion); err != nil || userVersion != SchemaV3 {
		t.Fatalf("user_version=%d err=%v", userVersion, err)
	}
	if err := control.db.QueryRow("SELECT COUNT(*) FROM schema_migrations").Scan(&migrationCount); err != nil || migrationCount != SchemaV3 {
		t.Fatalf("migration count=%d err=%v", migrationCount, err)
	}
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_cutover").Scan(&cutoverCount); err != nil || cutoverCount != len(controlDomainOrder) {
		t.Fatalf("cutover count=%d err=%v", cutoverCount, err)
	}
	var operationCount, operationTriggerCount int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_operations").Scan(&operationCount); err != nil || operationCount != 0 {
		t.Fatalf("operation count=%d err=%v", operationCount, err)
	}
	if err := control.db.QueryRow(
		`SELECT COUNT(*) FROM sqlite_schema
		 WHERE type = 'trigger' AND name IN ('control_operations_no_update', 'control_operations_no_delete')`,
	).Scan(&operationTriggerCount); err != nil || operationTriggerCount != len(controlOperationImmutabilityTriggers) {
		t.Fatalf("operation triggers=%d err=%v", operationTriggerCount, err)
	}

	var journalMode string
	if err := control.db.QueryRow("PRAGMA journal_mode").Scan(&journalMode); err != nil || journalMode != "wal" {
		t.Fatalf("journal mode=%q err=%v", journalMode, err)
	}
	var synchronous int
	if err := control.db.QueryRow("PRAGMA synchronous").Scan(&synchronous); err != nil || synchronous != 2 {
		t.Fatalf("synchronous=%d err=%v", synchronous, err)
	}
	var foreignKeys int
	if err := control.db.QueryRow("PRAGMA foreign_keys").Scan(&foreignKeys); err != nil || foreignKeys != 1 {
		t.Fatalf("foreign_keys=%d err=%v", foreignKeys, err)
	}
	var runtime, mode, owner string
	var token, leaseUntil int64
	if err := control.db.QueryRow(
		"SELECT runtime, mode, owner_instance_id, fencing_token, lease_until FROM control_writer WHERE singleton = 1",
	).Scan(&runtime, &mode, &owner, &token, &leaseUntil); err != nil {
		t.Fatal(err)
	}
	if runtime != RuntimeGo || mode != ModeShadow || owner != "owner-a" || token != 1 || leaseUntil <= 0 {
		t.Fatalf("writer row: %q %q %q %d %d", runtime, mode, owner, token, leaseUntil)
	}
}

func TestPutTransactionRollsBackWhenTheDatabaseRejectsTheWrite(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if err := control.Put(Record{
		Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"id":"p1"}`),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`
		CREATE TRIGGER reject_policy_disable
		BEFORE UPDATE ON policies
		WHEN NEW.state = 'DISABLED'
		BEGIN
			SELECT RAISE(ABORT, 'injected write failure');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if err := control.Put(Record{
		Domain: "policy", ID: "p1", Revision: 2, State: "DISABLED", Payload: json.RawMessage(`{"id":"p1"}`),
	}); err == nil {
		t.Fatal("trigger failure must abort Put")
	}
	if _, err := control.db.Exec("DROP TRIGGER reject_policy_disable"); err != nil {
		t.Fatal(err)
	}
	got, ok, err := control.Get("policy", "p1")
	if err != nil || !ok || got.Revision != 1 || got.State != "ACTIVE" {
		t.Fatalf("partial write escaped rollback: %+v ok=%v err=%v", got, ok, err)
	}
}

func TestInvalidOrCorruptDatabasePayloadFailsClosed(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if err := control.Put(Record{
		Domain: "policy", ID: "bad", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"broken":`),
	}); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("invalid payload: %v", err)
	}
	if err := control.Put(Record{
		Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"id":"p1"}`),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE policies SET record_digest = ? WHERE id = 'p1'", strings.Repeat("0", 64)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.Get("policy", "p1"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt payload: %v", err)
	}
}

func TestPayloadIsCanonicalAndBounded(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if err := control.Put(Record{
		Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE",
		Payload: json.RawMessage(" { \"z\" : 1, \"a\" : [ true ] } \n"),
	}); err != nil {
		t.Fatal(err)
	}
	got, ok, err := control.Get("policy", "p1")
	if err != nil || !ok || string(got.Payload) != `{"a":[true],"z":1}` {
		t.Fatalf("canonical payload: %q ok=%v err=%v", got.Payload, ok, err)
	}
	oversized := json.RawMessage(`{"value":"` + strings.Repeat("x", maximumPayloadBytes) + `"}`)
	if err := control.Put(Record{
		Domain: "policy", ID: "large", Revision: 1, State: "ACTIVE", Payload: oversized,
	}); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("oversized payload: %v", err)
	}
	if err := control.Put(Record{
		Domain: "policy", ID: "epoch", Revision: 1, ExecutionEpoch: math.MaxUint64, State: "ACTIVE",
	}); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("oversized epoch: %v", err)
	}
}

func TestControlPayloadRejectsSecretMaterialButAllowsReferences(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	for index, payload := range []json.RawMessage{
		json.RawMessage(`{"password":"do-not-store"}`),
		json.RawMessage(`{"nested":{"clientSecret":"do-not-store"}}`),
		json.RawMessage(`{"value":"AGE-SECRET-KEY-1EXAMPLE"}`),
		json.RawMessage(`{"value":"-----BEGIN PRIVATE KEY-----"}`),
		json.RawMessage(`{"value":"-----BEGIN RSA PRIVATE KEY-----"}`),
		json.RawMessage(`{"sessionToken":"do-not-store"}`),
		json.RawMessage(`{"api-token":"do-not-store"}`),
		json.RawMessage(`{"nested":[{"Pass-Word":"do-not-store"}]}`),
		json.RawMessage(`{"credential":"do-not-store"}`),
		json.RawMessage(`{"oauthSecret":"do-not-store"}`),
	} {
		if err := control.Put(Record{
			Domain: "policy", ID: fmt.Sprintf("secret-%d", index), Revision: 1, State: "ACTIVE", Payload: payload,
		}); !errors.Is(err, ErrSecretDetected) {
			t.Fatalf("secret payload %d: %v", index, err)
		}
	}
	if err := control.Put(Record{
		Domain:   "policy",
		ID:       "references",
		Revision: 1,
		State:    "ACTIVE",
		Payload: json.RawMessage(
			`{"credentialReference":"vault://prod","identityDigest":"sha256:abc","fencingToken":4,"secretId":"managed","credentialProvider":"vault"}`,
		),
	}); err != nil {
		t.Fatalf("non-secret references: %v", err)
	}
	for index := range 10 {
		if _, ok, err := control.Get("policy", fmt.Sprintf("secret-%d", index)); err != nil || ok {
			t.Fatalf("secret payload %d persisted: ok=%v err=%v", index, ok, err)
		}
	}
}

func TestControlEventJournalIsImmutableAndRequiredForReads(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if err := control.Put(Record{
		Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"id":"p1"}`),
	}); err != nil {
		t.Fatal(err)
	}
	if err := control.Put(Record{
		Domain: "policy", ID: "p1", Revision: 2, State: "DISABLED", Payload: json.RawMessage(`{"id":"p1"}`),
	}); err != nil {
		t.Fatal(err)
	}
	var eventCount int
	if err := control.db.QueryRow(
		"SELECT COUNT(*) FROM control_events WHERE domain = 'policy' AND record_id = 'p1'",
	).Scan(&eventCount); err != nil || eventCount != 2 {
		t.Fatalf("event count=%d err=%v", eventCount, err)
	}
	if _, err := control.db.Exec(
		"UPDATE control_events SET state = 'ACTIVE' WHERE domain = 'policy' AND record_id = 'p1' AND revision = 2",
	); err == nil {
		t.Fatal("event update must be mechanically denied")
	}
	if _, err := control.db.Exec(
		"DELETE FROM control_events WHERE domain = 'policy' AND record_id = 'p1' AND revision = 1",
	); err == nil {
		t.Fatal("event deletion must be mechanically denied")
	}

	if _, err := control.db.Exec("DROP TRIGGER control_events_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(
		"DELETE FROM control_events WHERE domain = 'policy' AND record_id = 'p1' AND revision = 1",
	); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlEventImmutabilityTriggers[1]); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.Get("policy", "p1"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("missing event must fail closed: %v", err)
	}
}

func TestSchemaAndLatestEventMetadataCorruptionFailClosed(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if err := control.Put(Record{
		Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(
		"UPDATE policies SET writer_fencing_token = writer_fencing_token + 1 WHERE id = 'p1'",
	); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.Get("policy", "p1"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("record/event metadata mismatch: %v", err)
	}

	second := openShadow(t)
	defer second.Close()
	if _, err := second.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := second.Get("policy", "missing"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing immutability trigger read: %v", err)
	}
	if err := second.Put(Record{
		Domain: "policy", ID: "p2", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
	}); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing immutability trigger: %v", err)
	}
}

func TestExistingForeignOrSymlinkedDatabaseIsRejected(t *testing.T) {
	foreign := t.TempDir()
	if err := os.WriteFile(filepath.Join(foreign, ControlDatabaseFilename), []byte("not sqlite"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: foreign, Owner: "owner-a"}); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("foreign database: %v", err)
	}

	target := filepath.Join(t.TempDir(), "outside.sqlite3")
	if err := os.WriteFile(target, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	symlinkStore := t.TempDir()
	symlink := filepath.Join(symlinkStore, ControlDatabaseFilename)
	if err := os.Symlink(target, symlink); err != nil {
		t.Skipf("symbolic links are unavailable: %v", err)
	}
	if _, err := OpenControl(OpenOptions{Path: symlinkStore, Owner: "owner-a"}); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("symlinked database: %v", err)
	}
}

func TestUnknownSQLiteUserObjectsAreRejectedBeforeWriteSideEffects(t *testing.T) {
	t.Run("extra table", func(t *testing.T) {
		assertCopiedForeignDatabaseUntouched(t, func(control *Control) error {
			_, err := control.db.Exec("CREATE TABLE attacker_notes(id INTEGER PRIMARY KEY)")
			return err
		})
	})

	t.Run("extra view", func(t *testing.T) {
		assertCopiedForeignDatabaseUntouched(t, func(control *Control) error {
			_, err := control.db.Exec("CREATE VIEW attacker_view AS SELECT 1 AS id")
			return err
		})
	})

	t.Run("schema v1 missing domain table", func(t *testing.T) {
		assertCopiedForeignDatabaseUntouched(t, func(control *Control) error {
			_, err := control.db.Exec("DROP TABLE forecasts")
			return err
		})
	})

	t.Run("expected table replaced by view", func(t *testing.T) {
		assertCopiedForeignDatabaseUntouched(t, func(control *Control) error {
			if _, err := control.db.Exec("DROP TABLE policies"); err != nil {
				return err
			}
			_, err := control.db.Exec("CREATE VIEW policies AS SELECT 'x' AS id")
			return err
		})
	})

	t.Run("empty metadata row", func(t *testing.T) {
		assertCopiedForeignDatabaseUntouched(t, func(control *Control) error {
			_, err := control.db.Exec("DELETE FROM control_store_meta")
			return err
		})
	})

	t.Run("extra migration row", func(t *testing.T) {
		assertCopiedForeignDatabaseUntouched(t, func(control *Control) error {
			_, err := control.db.Exec(
				"INSERT INTO schema_migrations(version, applied_at, description) VALUES(4, 1, 'foreign')",
			)
			return err
		})
	})
}

func TestValidateControlUserObjectsPropagatesQueryAndScanFailures(t *testing.T) {
	injected := errors.New("schema query failed")
	if err := validateControlUserObjects(failingQuerier{err: injected}, SchemaV1); !errors.Is(err, ErrForeignRuntimeStore) || !strings.Contains(err.Error(), injected.Error()) {
		t.Fatalf("query failure: %v", err)
	}

	control := openShadow(t)
	defer control.Close()
	if err := validateControlUserObjects(oneColumnQuerier{db: control.db}, SchemaV1); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("scan failure: %v", err)
	}
}

func TestControlPayloadRejectsExcessiveNesting(t *testing.T) {
	var value any = "leaf"
	for range 130 {
		value = []any{value}
	}
	if err := rejectControlSecretMaterial(value, 0); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("nesting: %v", err)
	}
}

type failingQuerier struct {
	err error
}

func (querier failingQuerier) Query(string, ...any) (*sql.Rows, error) {
	return nil, querier.err
}

type oneColumnQuerier struct {
	db interface {
		Query(query string, args ...any) (*sql.Rows, error)
	}
}

func (querier oneColumnQuerier) Query(string, ...any) (*sql.Rows, error) {
	return querier.db.Query("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
}

func assertCopiedForeignDatabaseUntouched(t *testing.T, mutate func(*Control) error) {
	t.Helper()
	control := openShadow(t)
	source := control.DatabasePath()
	if err := mutate(control); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	isolated := t.TempDir()
	databasePath := filepath.Join(isolated, ControlDatabaseFilename)
	data, err := os.ReadFile(source)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(databasePath, data, 0o600); err != nil {
		t.Fatal(err)
	}
	before, err := os.Stat(databasePath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: isolated, Owner: "owner-b"}); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("unknown or incomplete sqlite objects: %v", err)
	}
	after, err := os.Stat(databasePath)
	if err != nil {
		t.Fatal(err)
	}
	if after.Size() != before.Size() {
		t.Fatalf("foreign database size changed: before=%d after=%d", before.Size(), after.Size())
	}
	for _, suffix := range []string{"-wal", "-shm"} {
		if _, err := os.Lstat(databasePath + suffix); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("write sidecar %s: %v", suffix, err)
		}
	}
}

func TestMutableCompatibilityCatalogCannotRedirectSQL(t *testing.T) {
	originalTable := TableNames[0]
	originalDomain := DomainTable["policy"]
	TableNames[0] = "attacker_table"
	DomainTable["policy"] = "attacker_table"
	defer func() {
		TableNames[0] = originalTable
		DomainTable["policy"] = originalDomain
	}()

	control := openShadow(t)
	defer control.Close()
	if control.Tables()[0] != "policies" {
		t.Fatalf("private table catalog was redirected: %v", control.Tables())
	}
	if err := control.Put(Record{
		Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM policies WHERE id = 'p1'").Scan(&count); err != nil || count != 1 {
		t.Fatalf("trusted table write count=%d err=%v", count, err)
	}
}
