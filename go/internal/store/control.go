package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
	modernsqlite "modernc.org/sqlite"
)

const (
	ControlDatabaseFilename = "control.sqlite3"
	maximumPayloadBytes     = 1 << 20
)

var controlDomainOrder = [...]string{
	"policy",
	"target",
	"scheduler_run",
	"action",
	"risk",
	"wave",
	"peer",
	"grant",
	"session",
	"transfer",
	"forecast",
	"agent_run",
}

type OpenOptions struct {
	Path         string
	Owner        string
	Now          func() int64
	LeaseSeconds int64
}

type Record struct {
	Domain         string          `json:"domain"`
	ID             string          `json:"id"`
	Revision       int64           `json:"revision"`
	ExecutionEpoch uint64          `json:"executionEpoch"`
	State          string          `json:"state"`
	Payload        json.RawMessage `json:"payload"`
}

type WriterLease struct {
	Runtime         string `json:"runtime"`
	Mode            string `json:"mode"`
	OwnerInstanceID string `json:"ownerInstanceId"`
	FencingToken    int64  `json:"fencingToken"`
	LeaseUntil      int64  `json:"leaseUntil"`
}

type Snapshot struct {
	SchemaVersion int         `json:"schemaVersion"`
	Runtime       string      `json:"runtime"`
	Mode          string      `json:"mode"`
	Writer        WriterLease `json:"writer"`
	Records       []Record    `json:"records"`
	Digest        string      `json:"digest"`
}

type Control struct {
	mu                  sync.Mutex
	path                string
	databasePath        string
	owner               string
	token               int64
	leaseUntil          int64
	leaseSeconds        int64
	now                 func() int64
	schema              int
	db                  *sql.DB
	closed              bool
	admissionFaultStage string
}

type rowScanner interface {
	Scan(dest ...any) error
}

type storedRecordMetadata struct {
	writerToken int64
	timestamp   int64
}

func OpenControl(opts OpenOptions) (*Control, error) {
	if strings.TrimSpace(opts.Owner) == "" {
		return nil, ErrEmptyRecordID
	}
	if err := RejectPythonPath(opts.Path); err != nil {
		return nil, err
	}
	abs, err := filepath.Abs(opts.Path)
	if err != nil {
		return nil, err
	}
	if err := RejectPythonPath(abs); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(abs, 0o700); err != nil {
		return nil, err
	}
	resolved, err := filepath.EvalSymlinks(abs)
	if err != nil {
		return nil, err
	}
	if err := RejectPythonPath(resolved); err != nil {
		return nil, err
	}
	if err := rejectLegacyFileStore(resolved); err != nil {
		return nil, err
	}

	databasePath := filepath.Join(resolved, ControlDatabaseFilename)
	info, statErr := os.Lstat(databasePath)
	databaseExisted := statErr == nil && info.Size() > 0
	if statErr != nil && !errors.Is(statErr, os.ErrNotExist) {
		return nil, statErr
	}
	if statErr == nil && info.Mode()&os.ModeSymlink != 0 {
		return nil, fmt.Errorf("%w: database file must not be a symbolic link", ErrForeignRuntimeStore)
	}
	if statErr == nil && info.IsDir() {
		return nil, fmt.Errorf("%w: database path is a directory", ErrForeignRuntimeStore)
	}
	if databaseExisted {
		initialized, err := validateExistingControlMarker(databasePath)
		if err != nil {
			return nil, err
		}
		databaseExisted = initialized
	} else if statErr != nil {
		file, err := os.OpenFile(databasePath, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0o600)
		if err != nil {
			return nil, err
		}
		if err := file.Close(); err != nil {
			return nil, err
		}
	}

	connector, err := modernsqlite.NewConnector(controlDatabaseDSN(databasePath))
	if err != nil {
		return nil, err
	}
	db := sql.OpenDB(connector)
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(0)
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, err
	}
	if err := os.Chmod(databasePath, 0o600); err != nil {
		_ = db.Close()
		return nil, err
	}

	nowFn := opts.Now
	if nowFn == nil {
		nowFn = func() int64 { return time.Now().Unix() }
	}
	leaseSeconds := opts.LeaseSeconds
	if leaseSeconds <= 0 {
		leaseSeconds = 30
	}
	store := &Control{
		path:         resolved,
		databasePath: databasePath,
		owner:        opts.Owner,
		leaseSeconds: leaseSeconds,
		now:          nowFn,
		db:           db,
	}
	if err := store.bootstrapAndClaim(databaseExisted); err != nil {
		_ = db.Close()
		return nil, err
	}
	return store, nil
}

func controlDatabaseDSN(databasePath string) string {
	query := hardenedControlQuery()
	query.Set("_foreign_keys", "1")
	query.Set("_journal_mode", "WAL")
	query.Set("_synchronous", "FULL")
	query.Set("_txlock", "immediate")
	return controlDatabaseURL(databasePath, query)
}

func readOnlyControlDatabaseDSN(databasePath string) string {
	query := hardenedControlQuery()
	query.Set("_query_only", "1")
	query.Set("mode", "ro")
	query.Set("immutable", "1")
	return controlDatabaseURL(databasePath, query)
}

func hardenedControlQuery() url.Values {
	query := url.Values{}
	query.Set("_busy_timeout", "30000")
	query.Set("_defensive", "1")
	query.Set("_dqs", "0")
	query.Set("_pragma", "trusted_schema(OFF)")
	return query
}

func controlDatabaseURL(databasePath string, query url.Values) string {
	path := filepath.ToSlash(databasePath)
	if runtime.GOOS == "windows" && len(path) >= 2 && path[1] == ':' {
		path = "/" + path
	}
	return (&url.URL{Scheme: "file", Path: path, RawQuery: query.Encode()}).String()
}

func validateExistingControlMarker(databasePath string) (bool, error) {
	// Identity checks stay on a read-only connection so a foreign database cannot
	// be mutated by WAL or writer-lease side effects before it is rejected.
	connector, err := modernsqlite.NewConnector(readOnlyControlDatabaseDSN(databasePath))
	if err != nil {
		return false, err
	}
	db := sql.OpenDB(connector)
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	var objectCount int
	queryErr := db.QueryRow(
		"SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'",
	).Scan(&objectCount)
	if queryErr == nil && objectCount == 0 {
		closeErr := db.Close()
		if closeErr != nil {
			return false, closeErr
		}
		return false, nil
	}
	var markerCount int
	if queryErr == nil {
		queryErr = db.QueryRow(
			`SELECT COUNT(*) FROM sqlite_schema
		 WHERE type = 'table'
		   AND name IN ('control_store_meta', 'control_writer', 'schema_migrations')`,
		).Scan(&markerCount)
	}
	if queryErr == nil && markerCount != len(bootstrapSchemaStatements) {
		queryErr = ErrForeignRuntimeStore
	}
	var runtimeName, mode, uniqueWriter string
	var schema int
	if queryErr == nil {
		queryErr = db.QueryRow(
			"SELECT runtime, mode, schema_version, unique_writer FROM control_store_meta WHERE singleton = 1",
		).Scan(&runtimeName, &mode, &schema, &uniqueWriter)
	}
	if queryErr == nil && (runtimeName != RuntimeGo || mode != ModeShadow ||
		uniqueWriter != RuntimeGo || schema < 0 || schema > CurrentSchema) {
		queryErr = ErrForeignRuntimeStore
	}
	var userVersion, migrationCount int
	if queryErr == nil {
		queryErr = db.QueryRow("PRAGMA user_version").Scan(&userVersion)
	}
	if queryErr == nil {
		queryErr = db.QueryRow("SELECT COUNT(*) FROM schema_migrations").Scan(&migrationCount)
	}
	if queryErr == nil && (userVersion != schema || migrationCount != schema) {
		queryErr = ErrForeignRuntimeStore
	}
	if queryErr == nil {
		queryErr = validateControlUserObjects(db, schema)
	}
	closeErr := db.Close()
	if errors.Is(queryErr, sql.ErrNoRows) {
		queryErr = ErrForeignRuntimeStore
	}
	if queryErr != nil {
		if !errors.Is(queryErr, ErrForeignRuntimeStore) {
			queryErr = fmt.Errorf("%w: read database marker: %v", ErrForeignRuntimeStore, queryErr)
		}
		if closeErr != nil {
			return false, errors.Join(queryErr, closeErr)
		}
		return false, queryErr
	}
	if closeErr != nil {
		return false, closeErr
	}
	return true, nil
}

type sqliteQuerier interface {
	Query(query string, args ...any) (*sql.Rows, error)
}

func expectedControlUserObjects(schema int) map[string]string {
	objects := map[string]string{
		"control_store_meta": "table",
		"control_writer":     "table",
		"schema_migrations":  "table",
	}
	if schema >= SchemaV6 {
		for name := range actionReconciliationSchemaObjects {
			objects[name] = "trigger"
		}
		objects["action_reconciliation_boundary"] = "table"
	}
	if schema < SchemaV1 {
		return objects
	}
	for _, table := range controlTableNames {
		objects[table] = "table"
	}
	objects["control_events"] = "table"
	objects["control_events_no_update"] = "trigger"
	objects["control_events_no_delete"] = "trigger"
	if schema < SchemaV2 {
		return objects
	}
	objects["control_cutover"] = "table"
	objects["control_cutover_events"] = "table"
	objects["control_cutover_no_delete"] = "trigger"
	objects["control_cutover_events_no_update"] = "trigger"
	objects["control_cutover_events_no_delete"] = "trigger"
	if schema < SchemaV3 {
		return objects
	}
	objects["control_operations"] = "table"
	objects["control_operations_no_update"] = "trigger"
	objects["control_operations_no_delete"] = "trigger"
	if schema >= SchemaV4 {
		for name := range storageDispatchSchemaObjects {
			kind := "trigger"
			if name == "storage_dispatches" {
				kind = "table"
			}
			objects[name] = kind
		}
	}
	if schema >= SchemaV5 {
		for name := range actionAdmissionSchemaObjects {
			kind := "trigger"
			if name == "action_leases" || name == "action_lease_events" || name == "action_resource_leases" {
				kind = "table"
			} else if name == "idx_action_resource_leases_action" {
				kind = "index"
			}
			objects[name] = kind
		}
	}
	return objects
}

func validateControlUserObjects(querier sqliteQuerier, schema int) error {
	expected := expectedControlUserObjects(schema)
	rows, err := querier.Query("SELECT name, type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
	if err != nil {
		return fmt.Errorf("%w: list sqlite objects: %v", ErrForeignRuntimeStore, err)
	}
	defer rows.Close()
	seen := make(map[string]string, len(expected))
	for rows.Next() {
		var name, objectType string
		if err := rows.Scan(&name, &objectType); err != nil {
			return fmt.Errorf("%w: read sqlite object: %v", ErrForeignRuntimeStore, err)
		}
		wantType, ok := expected[name]
		if !ok {
			return fmt.Errorf("%w: unexpected sqlite %s %q", ErrForeignRuntimeStore, objectType, name)
		}
		if wantType != objectType {
			return fmt.Errorf("%w: sqlite object %q type %s", ErrForeignRuntimeStore, name, objectType)
		}
		seen[name] = objectType
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("%w: iterate sqlite objects: %v", ErrForeignRuntimeStore, err)
	}
	if err := rows.Close(); err != nil {
		return fmt.Errorf("%w: close sqlite object cursor: %v", ErrForeignRuntimeStore, err)
	}
	if len(seen) != len(expected) {
		return fmt.Errorf("%w: incomplete sqlite object set", ErrForeignRuntimeStore)
	}
	return nil
}

func rejectLegacyFileStore(path string) error {
	legacyNames := append([]string{"manifest.json", "writer.json"}, controlTableNames[:]...)
	for _, name := range legacyNames {
		_, err := os.Lstat(filepath.Join(path, name))
		if err == nil {
			return fmt.Errorf("%w: %s", ErrLegacyFileStore, name)
		}
		if !errors.Is(err, os.ErrNotExist) {
			return err
		}
	}
	return nil
}

func (store *Control) bootstrapAndClaim(databaseExisted bool) error {
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	if databaseExisted {
		var marker int
		err := tx.QueryRow(
			"SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'control_store_meta'",
		).Scan(&marker)
		if errors.Is(err, sql.ErrNoRows) {
			return ErrForeignRuntimeStore
		}
		if err != nil {
			return fmt.Errorf("%w: read database marker: %v", ErrForeignRuntimeStore, err)
		}
	}
	for _, statement := range bootstrapSchemaStatements {
		if _, err := tx.Exec(statement); err != nil {
			return fmt.Errorf("%w: bootstrap schema: %v", ErrForeignRuntimeStore, err)
		}
	}

	var metaCount int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_store_meta").Scan(&metaCount); err != nil {
		return fmt.Errorf("%w: read store metadata: %v", ErrForeignRuntimeStore, err)
	}
	if metaCount == 0 {
		if databaseExisted {
			return ErrForeignRuntimeStore
		}
		if _, err := tx.Exec(
			"INSERT INTO control_store_meta(singleton, runtime, mode, schema_version, unique_writer) VALUES(1, ?, ?, 0, ?)",
			RuntimeGo,
			ModeShadow,
			RuntimeGo,
		); err != nil {
			return err
		}
	} else if metaCount != 1 {
		return ErrForeignRuntimeStore
	}

	var runtimeName, mode, uniqueWriter string
	if err := tx.QueryRow(
		"SELECT runtime, mode, schema_version, unique_writer FROM control_store_meta WHERE singleton = 1",
	).Scan(&runtimeName, &mode, &store.schema, &uniqueWriter); err != nil {
		return fmt.Errorf("%w: invalid store metadata: %v", ErrForeignRuntimeStore, err)
	}
	if runtimeName != RuntimeGo || mode != ModeShadow || uniqueWriter != RuntimeGo ||
		store.schema < 0 || store.schema > CurrentSchema {
		return ErrForeignRuntimeStore
	}
	var userVersion, migrationCount int
	if err := tx.QueryRow("PRAGMA user_version").Scan(&userVersion); err != nil {
		return fmt.Errorf("%w: read user_version: %v", ErrForeignRuntimeStore, err)
	}
	if err := tx.QueryRow("SELECT COUNT(*) FROM schema_migrations").Scan(&migrationCount); err != nil {
		return fmt.Errorf("%w: read schema_migrations: %v", ErrForeignRuntimeStore, err)
	}
	if userVersion != store.schema || migrationCount != store.schema {
		return ErrForeignRuntimeStore
	}
	if err := validateControlUserObjects(tx, store.schema); err != nil {
		return err
	}

	if err := store.claimWriterTx(tx); err != nil {
		return err
	}
	if err := store.migrateTx(tx); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	return nil
}

var bootstrapSchemaStatements = [...]string{
	`CREATE TABLE IF NOT EXISTS control_store_meta (
		singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
		runtime TEXT NOT NULL,
		mode TEXT NOT NULL,
		schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 3),
		unique_writer TEXT NOT NULL
	) STRICT`,
	`CREATE TABLE IF NOT EXISTS control_writer (
		singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
		runtime TEXT NOT NULL,
		mode TEXT NOT NULL,
		owner_instance_id TEXT NOT NULL CHECK(length(owner_instance_id) > 0),
		fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
		lease_until INTEGER NOT NULL CHECK(lease_until >= 0)
	) STRICT`,
	`CREATE TABLE IF NOT EXISTS schema_migrations (
		version INTEGER PRIMARY KEY CHECK(version > 0),
		applied_at INTEGER NOT NULL CHECK(applied_at >= 0),
		description TEXT NOT NULL CHECK(length(description) > 0)
	) STRICT`,
}

func (store *Control) claimWriterTx(tx *sql.Tx) error {
	now := store.now()
	leaseUntil, err := store.nextLeaseUntil(now)
	if err != nil {
		return err
	}
	var runtimeName, mode, owner string
	var token, currentLeaseUntil int64
	err = tx.QueryRow(
		"SELECT runtime, mode, owner_instance_id, fencing_token, lease_until FROM control_writer WHERE singleton = 1",
	).Scan(&runtimeName, &mode, &owner, &token, &currentLeaseUntil)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		store.token = 1
	case err != nil:
		return fmt.Errorf("%w: invalid writer row: %v", ErrForeignRuntimeStore, err)
	default:
		if runtimeName != RuntimeGo || mode != ModeShadow || owner == "" || token <= 0 || currentLeaseUntil < 0 {
			return ErrForeignRuntimeStore
		}
		if now < currentLeaseUntil {
			return ErrWriterFenceHeld
		}
		if token == math.MaxInt64 {
			return ErrWriterFenceHeld
		}
		store.token = token + 1
	}
	if _, err := tx.Exec(
		`INSERT INTO control_writer(singleton, runtime, mode, owner_instance_id, fencing_token, lease_until)
		 VALUES(1, ?, ?, ?, ?, ?)
		 ON CONFLICT(singleton) DO UPDATE SET
			runtime = excluded.runtime,
			mode = excluded.mode,
			owner_instance_id = excluded.owner_instance_id,
			fencing_token = excluded.fencing_token,
			lease_until = excluded.lease_until`,
		RuntimeGo,
		ModeShadow,
		store.owner,
		store.token,
		leaseUntil,
	); err != nil {
		return err
	}
	store.leaseUntil = leaseUntil
	return nil
}

func (store *Control) migrateTx(tx *sql.Tx) error {
	if store.schema > CurrentSchema || store.schema < 0 {
		return ErrForeignRuntimeStore
	}
	if store.schema == 0 {
		if err := store.migrateToV1Tx(tx); err != nil {
			return err
		}
	}
	if store.schema == SchemaV1 {
		if err := store.migrateToV2Tx(tx); err != nil {
			return err
		}
	}
	if store.schema == SchemaV2 {
		if err := store.migrateToV3Tx(tx); err != nil {
			return err
		}
	}
	if store.schema == SchemaV3 {
		if err := store.migrateToV4Tx(tx); err != nil {
			return err
		}
	}
	if store.schema == SchemaV4 {
		if err := store.migrateToV5Tx(tx); err != nil {
			return err
		}
	}
	if store.schema == SchemaV5 {
		if err := store.migrateToV6Tx(tx); err != nil {
			return err
		}
	}
	return verifySchemaTx(tx, store.schema)
}

func (store *Control) migrateToV1Tx(tx *sql.Tx) error {
	for _, table := range controlTableNames {
		if _, err := tx.Exec(createDomainTableSQL(table)); err != nil {
			return fmt.Errorf("create control table %s: %w", table, err)
		}
	}
	if _, err := tx.Exec(controlEventsSchema); err != nil {
		return fmt.Errorf("create control event journal: %w", err)
	}
	for _, statement := range controlEventImmutabilityTriggers {
		if _, err := tx.Exec(statement); err != nil {
			return fmt.Errorf("create control event immutability trigger: %w", err)
		}
	}
	if _, err := tx.Exec(
		"INSERT INTO schema_migrations(version, applied_at, description) VALUES(1, ?, ?)",
		store.now(),
		"create strict domain tables, append-only event journal, and fenced writer metadata",
	); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version = 1 WHERE singleton = 1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version = 1"); err != nil {
		return err
	}
	store.schema = SchemaV1
	return verifySchemaTx(tx, store.schema)
}

func (store *Control) migrateToV2Tx(tx *sql.Tx) error {
	if _, err := tx.Exec(`CREATE TABLE control_store_meta_v2 (
		singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
		runtime TEXT NOT NULL,
		mode TEXT NOT NULL,
		schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 2),
		unique_writer TEXT NOT NULL
	) STRICT`); err != nil {
		return fmt.Errorf("create control store metadata v2: %w", err)
	}
	if _, err := tx.Exec(
		`INSERT INTO control_store_meta_v2(singleton, runtime, mode, schema_version, unique_writer)
		 SELECT singleton, runtime, mode, schema_version, unique_writer FROM control_store_meta`,
	); err != nil {
		return fmt.Errorf("copy control store metadata: %w", err)
	}
	if _, err := tx.Exec("DROP TABLE control_store_meta"); err != nil {
		return err
	}
	if _, err := tx.Exec("ALTER TABLE control_store_meta_v2 RENAME TO control_store_meta"); err != nil {
		return err
	}
	if _, err := tx.Exec(controlCutoverSchema); err != nil {
		return fmt.Errorf("create control cutover table: %w", err)
	}
	if _, err := tx.Exec(controlCutoverEventsSchema); err != nil {
		return fmt.Errorf("create control cutover journal: %w", err)
	}
	for _, statement := range controlCutoverImmutabilityTriggers {
		if _, err := tx.Exec(statement); err != nil {
			return fmt.Errorf("create control cutover immutability trigger: %w", err)
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	for _, domain := range controlDomainOrder {
		if _, err := tx.Exec(
			`INSERT INTO control_cutover(
				domain, state, revision, epoch, fencing_token, previous_owner, owner,
				transfer_id, writer_fencing_token, updated_at
			) VALUES(?, ?, 1, 1, ?, ?, ?, ?, ?, ?)`,
			domain,
			string(CutoverShadow),
			store.token,
			OwnerPython,
			OwnerPython,
			CutoverGenesisTransferID,
			store.token,
			now,
		); err != nil {
			return fmt.Errorf("seed cutover %s: %w", domain, err)
		}
		if _, err := tx.Exec(
			`INSERT INTO control_cutover_events(
				domain, transfer_id, previous_state, state, previous_revision, revision,
				previous_epoch, epoch, previous_fencing_token, fencing_token,
				previous_owner, owner, writer_fencing_token, recorded_at
			) VALUES(?, ?, '', ?, 0, 1, 0, 1, 0, ?, ?, ?, ?, ?)`,
			domain,
			CutoverGenesisTransferID,
			string(CutoverShadow),
			store.token,
			OwnerPython,
			OwnerPython,
			store.token,
			now,
		); err != nil {
			return fmt.Errorf("seed cutover event %s: %w", domain, err)
		}
	}
	if _, err := tx.Exec(
		"INSERT INTO schema_migrations(version, applied_at, description) VALUES(2, ?, ?)",
		now,
		"create per-domain cutover state and append-only cutover journal",
	); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version = 2 WHERE singleton = 1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version = 2"); err != nil {
		return err
	}
	store.schema = SchemaV2
	return nil
}

func (store *Control) migrateToV3Tx(tx *sql.Tx) error {
	if _, err := tx.Exec(`CREATE TABLE control_store_meta_v3 (
		singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
		runtime TEXT NOT NULL,
		mode TEXT NOT NULL,
		schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 3),
		unique_writer TEXT NOT NULL
	) STRICT`); err != nil {
		return fmt.Errorf("create control store metadata v3: %w", err)
	}
	if _, err := tx.Exec(
		`INSERT INTO control_store_meta_v3(singleton, runtime, mode, schema_version, unique_writer)
		 SELECT singleton, runtime, mode, schema_version, unique_writer FROM control_store_meta`,
	); err != nil {
		return fmt.Errorf("copy control store metadata v3: %w", err)
	}
	if _, err := tx.Exec("DROP TABLE control_store_meta"); err != nil {
		return err
	}
	if _, err := tx.Exec("ALTER TABLE control_store_meta_v3 RENAME TO control_store_meta"); err != nil {
		return err
	}
	if _, err := tx.Exec(controlOperationsSchema); err != nil {
		return fmt.Errorf("create control operation journal: %w", err)
	}
	for _, statement := range controlOperationImmutabilityTriggers {
		if _, err := tx.Exec(statement); err != nil {
			return fmt.Errorf("create control operation immutability trigger: %w", err)
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec(
		"INSERT INTO schema_migrations(version, applied_at, description) VALUES(3, ?, ?)",
		now,
		"create append-only replay-safe control operation journal",
	); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version = 3 WHERE singleton = 1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version = 3"); err != nil {
		return err
	}
	store.schema = SchemaV3
	return nil
}

func createDomainTableSQL(table string) string {
	return fmt.Sprintf(`CREATE TABLE %s (
		id TEXT PRIMARY KEY CHECK(length(id) > 0),
		revision INTEGER NOT NULL CHECK(revision > 0),
		execution_epoch INTEGER NOT NULL CHECK(execution_epoch >= 0),
		state TEXT NOT NULL CHECK(length(state) > 0),
		payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
		record_digest TEXT NOT NULL CHECK(length(record_digest) = 64 AND record_digest = lower(record_digest)),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token > 0),
		updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
	) STRICT`, table)
}

const controlEventsSchema = `CREATE TABLE control_events (
	event_id INTEGER PRIMARY KEY AUTOINCREMENT,
	domain TEXT NOT NULL CHECK(domain IN (
		'policy', 'target', 'scheduler_run', 'action', 'risk', 'wave',
		'peer', 'grant', 'session', 'transfer', 'forecast', 'agent_run'
	)),
	record_id TEXT NOT NULL CHECK(length(record_id) > 0),
	revision INTEGER NOT NULL CHECK(revision > 0),
	execution_epoch INTEGER NOT NULL CHECK(execution_epoch >= 0),
	state TEXT NOT NULL CHECK(length(state) > 0),
	payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
	record_digest TEXT NOT NULL CHECK(length(record_digest) = 64 AND record_digest = lower(record_digest)),
	writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token > 0),
	recorded_at INTEGER NOT NULL CHECK(recorded_at >= 0),
	UNIQUE(domain, record_id, revision)
) STRICT`

var controlEventImmutabilityTriggers = [...]string{
	`CREATE TRIGGER control_events_no_update
	BEFORE UPDATE ON control_events
	BEGIN
		SELECT RAISE(ABORT, 'CONTROL_EVENT_IMMUTABLE');
	END`,
	`CREATE TRIGGER control_events_no_delete
	BEFORE DELETE ON control_events
	BEGIN
		SELECT RAISE(ABORT, 'CONTROL_EVENT_IMMUTABLE');
	END`,
}

const controlCutoverSchema = `CREATE TABLE control_cutover (
	domain TEXT PRIMARY KEY CHECK(domain IN (
		'policy', 'target', 'scheduler_run', 'action', 'risk', 'wave',
		'peer', 'grant', 'session', 'transfer', 'forecast', 'agent_run'
	)),
	state TEXT NOT NULL CHECK(state IN (
		'shadow', 'dual_evaluate', 'go_authoritative', 'python_shadow', 'python_disabled'
	)),
	revision INTEGER NOT NULL CHECK(revision > 0),
	epoch INTEGER NOT NULL CHECK(epoch > 0),
	fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
	previous_owner TEXT NOT NULL CHECK(previous_owner IN ('python', 'go')),
	owner TEXT NOT NULL CHECK(owner IN ('python', 'go')),
	transfer_id TEXT NOT NULL CHECK(length(transfer_id) > 0),
	writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token > 0),
	updated_at INTEGER NOT NULL CHECK(updated_at >= 0)
) STRICT`

const controlCutoverEventsSchema = `CREATE TABLE control_cutover_events (
	event_id INTEGER PRIMARY KEY AUTOINCREMENT,
	domain TEXT NOT NULL CHECK(domain IN (
		'policy', 'target', 'scheduler_run', 'action', 'risk', 'wave',
		'peer', 'grant', 'session', 'transfer', 'forecast', 'agent_run'
	)),
	transfer_id TEXT NOT NULL CHECK(length(transfer_id) > 0),
	previous_state TEXT NOT NULL,
	state TEXT NOT NULL CHECK(state IN (
		'shadow', 'dual_evaluate', 'go_authoritative', 'python_shadow', 'python_disabled'
	)),
	previous_revision INTEGER NOT NULL CHECK(previous_revision >= 0),
	revision INTEGER NOT NULL CHECK(revision > 0),
	previous_epoch INTEGER NOT NULL CHECK(previous_epoch >= 0),
	epoch INTEGER NOT NULL CHECK(epoch > 0),
	previous_fencing_token INTEGER NOT NULL CHECK(previous_fencing_token >= 0),
	fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
	previous_owner TEXT NOT NULL CHECK(previous_owner IN ('python', 'go')),
	owner TEXT NOT NULL CHECK(owner IN ('python', 'go')),
	writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token > 0),
	recorded_at INTEGER NOT NULL CHECK(recorded_at >= 0),
	UNIQUE(domain, revision),
	UNIQUE(domain, transfer_id)
) STRICT`

var controlCutoverImmutabilityTriggers = [...]string{
	`CREATE TRIGGER control_cutover_no_delete
	BEFORE DELETE ON control_cutover
	BEGIN
		SELECT RAISE(ABORT, 'CONTROL_CUTOVER_IMMUTABLE');
	END`,
	`CREATE TRIGGER control_cutover_events_no_update
	BEFORE UPDATE ON control_cutover_events
	BEGIN
		SELECT RAISE(ABORT, 'CONTROL_CUTOVER_EVENT_IMMUTABLE');
	END`,
	`CREATE TRIGGER control_cutover_events_no_delete
	BEFORE DELETE ON control_cutover_events
	BEGIN
		SELECT RAISE(ABORT, 'CONTROL_CUTOVER_EVENT_IMMUTABLE');
	END`,
}

const controlOperationsSchema = `CREATE TABLE control_operations (
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
) STRICT`

var controlOperationImmutabilityTriggers = [...]string{
	`CREATE TRIGGER control_operations_no_update
	BEFORE UPDATE ON control_operations
	BEGIN
		SELECT RAISE(ABORT, 'CONTROL_OPERATION_IMMUTABLE');
	END`,
	`CREATE TRIGGER control_operations_no_delete
	BEFORE DELETE ON control_operations
	BEGIN
		SELECT RAISE(ABORT, 'CONTROL_OPERATION_IMMUTABLE');
	END`,
}

func verifySchemaTx(tx *sql.Tx, schema int) error {
	if schema == 0 {
		return nil
	}
	if schema < SchemaV1 || schema > CurrentSchema {
		return ErrForeignRuntimeStore
	}
	tables := append(append([]string(nil), controlTableNames[:]...), "control_events")
	if schema >= SchemaV2 {
		tables = append(tables, "control_cutover", "control_cutover_events")
	}
	if schema >= SchemaV3 {
		tables = append(tables, "control_operations")
	}
	if schema >= SchemaV4 {
		if err := verifyStorageDispatchSchemaTx(tx); err != nil {
			return err
		}
	}
	if schema >= SchemaV5 {
		if err := verifyActionAdmissionSchemaTx(tx); err != nil {
			return err
		}
	}
	if schema >= SchemaV6 {
		if err := verifyActionReconciliationSchemaTx(tx); err != nil {
			return err
		}
	}
	for _, table := range tables {
		var marker int
		if err := tx.QueryRow(
			"SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
			table,
		).Scan(&marker); err != nil {
			return fmt.Errorf("%w: missing table %s", ErrForeignRuntimeStore, table)
		}
	}
	var runtimeName, mode, uniqueWriter string
	var storedSchema, metaCount int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_store_meta").Scan(&metaCount); err != nil {
		return err
	}
	if err := tx.QueryRow(
		"SELECT runtime, mode, schema_version, unique_writer FROM control_store_meta WHERE singleton = 1",
	).Scan(&runtimeName, &mode, &storedSchema, &uniqueWriter); err != nil {
		return err
	}
	if metaCount != 1 || runtimeName != RuntimeGo || mode != ModeShadow ||
		storedSchema != schema || uniqueWriter != RuntimeGo {
		return ErrForeignRuntimeStore
	}
	var writerRuntime, writerMode, writerOwner string
	var writerToken, writerLease int64
	if err := tx.QueryRow(
		"SELECT runtime, mode, owner_instance_id, fencing_token, lease_until FROM control_writer WHERE singleton = 1",
	).Scan(&writerRuntime, &writerMode, &writerOwner, &writerToken, &writerLease); err != nil {
		return err
	}
	if writerRuntime != RuntimeGo || writerMode != ModeShadow || writerOwner == "" ||
		writerToken < 1 || writerLease < 0 {
		return ErrForeignRuntimeStore
	}
	var migrationCount, maximumMigration, userVersion int
	if err := tx.QueryRow(
		"SELECT COUNT(*), COALESCE(MAX(version), 0) FROM schema_migrations",
	).Scan(&migrationCount, &maximumMigration); err != nil {
		return err
	}
	if err := tx.QueryRow("PRAGMA user_version").Scan(&userVersion); err != nil {
		return err
	}
	if migrationCount != schema || maximumMigration != schema || userVersion != schema {
		return ErrForeignRuntimeStore
	}
	var triggerCount int
	if err := tx.QueryRow(
		`SELECT COUNT(*) FROM sqlite_schema
		 WHERE type = 'trigger' AND name IN ('control_events_no_update', 'control_events_no_delete')`,
	).Scan(&triggerCount); err != nil {
		return err
	}
	if triggerCount != len(controlEventImmutabilityTriggers) {
		return fmt.Errorf("%w: missing control event immutability trigger", ErrForeignRuntimeStore)
	}
	if schema >= SchemaV2 {
		var cutoverTriggerCount, cutoverRows int
		if err := tx.QueryRow(
			`SELECT COUNT(*) FROM sqlite_schema
			 WHERE type = 'trigger' AND name IN (
				'control_cutover_no_delete',
				'control_cutover_events_no_update',
				'control_cutover_events_no_delete'
			 )`,
		).Scan(&cutoverTriggerCount); err != nil {
			return err
		}
		if cutoverTriggerCount != len(controlCutoverImmutabilityTriggers) {
			return fmt.Errorf("%w: missing control cutover immutability trigger", ErrForeignRuntimeStore)
		}
		if err := tx.QueryRow("SELECT COUNT(*) FROM control_cutover").Scan(&cutoverRows); err != nil {
			return err
		}
		if cutoverRows != len(controlDomainOrder) {
			return fmt.Errorf("%w: incomplete control cutover rows", ErrForeignRuntimeStore)
		}
	}
	if schema >= SchemaV3 {
		var operationTriggerCount int
		if err := tx.QueryRow(
			`SELECT COUNT(*) FROM sqlite_schema
			 WHERE type = 'trigger' AND name IN (
				'control_operations_no_update',
				'control_operations_no_delete'
			 )`,
		).Scan(&operationTriggerCount); err != nil {
			return err
		}
		if operationTriggerCount != len(controlOperationImmutabilityTriggers) {
			return fmt.Errorf("%w: missing control operation immutability trigger", ErrForeignRuntimeStore)
		}
	}
	return nil
}

func (store *Control) Close() error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return nil
	}
	store.closed = true
	now := store.now()
	if now < 0 {
		now = 0
	}
	_, releaseErr := store.db.Exec(
		`UPDATE control_writer
		 SET lease_until = ?
		 WHERE singleton = 1 AND runtime = ? AND mode = ?
		   AND owner_instance_id = ? AND fencing_token = ?`,
		now,
		RuntimeGo,
		ModeShadow,
		store.owner,
		store.token,
	)
	closeErr := store.db.Close()
	return errors.Join(releaseErr, closeErr)
}

func (store *Control) Writer() WriterLease {
	store.mu.Lock()
	defer store.mu.Unlock()
	return WriterLease{
		Runtime:         RuntimeGo,
		Mode:            ModeShadow,
		OwnerInstanceID: store.owner,
		FencingToken:    store.token,
		LeaseUntil:      store.leaseUntil,
	}
}

func (store *Control) Tables() []string {
	return append([]string(nil), controlTableNames[:]...)
}

func (store *Control) SchemaVersion() int {
	store.mu.Lock()
	defer store.mu.Unlock()
	return store.schema
}

func (store *Control) DatabasePath() string {
	store.mu.Lock()
	defer store.mu.Unlock()
	return store.databasePath
}

func (store *Control) Put(record Record) error {
	return store.putControlRecord(record, nil)
}

func (store *Control) putControlRecord(record Record, dispatch *StorageDispatchIntent) error {
	return store.putLeasedControlRecord(record, dispatch, "")
}

func (store *Control) putLeasedControlRecord(record Record, dispatch *StorageDispatchIntent, claimToken string) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return ErrWriterFenceHeld
	}
	write, err := prepareControlRecordWrite(record, dispatch)
	if err != nil {
		return err
	}
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := store.now()
	leaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return err
	}
	if store.schema != CurrentSchema {
		return ErrSchemaInactive
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return err
	}
	var actionLeaseUntil int64
	if claimToken != "" {
		lease, err := readActiveActionLeaseTx(tx, record.ID)
		if err != nil {
			return err
		}
		if lease.Epoch != record.ExecutionEpoch || lease.WriterFencingToken != store.token {
			return ErrActionLeaseStale
		}
		if lease.ClaimToken != claimToken {
			return ErrInvalidClaimToken
		}
		if lease.LeaseUntil <= now {
			return ErrActionLeaseExpired
		}
		if _, _, err := validateActionLeaseResourcesTx(tx, lease); err != nil {
			return err
		}
		write.allowBoundLease = true
		actionLeaseUntil = lease.LeaseUntil
	}
	if err := store.putControlRecordTx(tx, write, now); err != nil {
		return err
	}
	commitNow := store.now()
	if commitNow < 0 || commitNow >= leaseUntil {
		return ErrWriterFenceHeld
	}
	if actionLeaseUntil != 0 && (commitNow < now || commitNow >= actionLeaseUntil) {
		return ErrActionLeaseExpired
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	store.leaseUntil = leaseUntil
	return nil
}

// A prepared write owns its canonical bytes, including a dispatch's operation
// identity. It cannot be rebound by modifying the caller's payload or intent.
type controlRecordWrite struct {
	record          Record
	table           string
	intentJSON      []byte
	intentDigest    string
	operationID     string
	allowBoundLease bool
}

func prepareControlRecordWrite(record Record, dispatch *StorageDispatchIntent) (controlRecordWrite, error) {
	if !ValidRecordID(record.ID) {
		return controlRecordWrite{}, ErrEmptyRecordID
	}
	table, ok := tableForDomain(record.Domain)
	if !ok {
		return controlRecordWrite{}, ErrUnknownDomain
	}
	if record.ExecutionEpoch > math.MaxInt64 {
		return controlRecordWrite{}, ErrEpochOutOfRange
	}
	payload, err := canonicalControlPayload(record.Payload)
	if err != nil {
		return controlRecordWrite{}, err
	}
	record.Payload = payload
	write := controlRecordWrite{record: record, table: table}
	if dispatch != nil {
		if record.Domain != "action" || record.State != "EXECUTING" || dispatch.ActionID != record.ID || dispatch.ExecutionEpoch != record.ExecutionEpoch {
			return controlRecordWrite{}, ErrInvalidStorageIntent
		}
		write.intentJSON, write.intentDigest, err = encodeStorageDispatchIntent(*dispatch)
		if err != nil {
			return controlRecordWrite{}, err
		}
		write.operationID = dispatch.OperationID
	}
	return write, nil
}

// putControlRecordTx does not own the transaction or writer lease. The caller
// holds store.mu, asserts its live writer and schema, then commits all admission
// reservations and this journal write together. Never call sql.DB here:
// https://go.dev/doc/database/execute-transactions#best-practices
func (store *Control) putControlRecordTx(tx *sql.Tx, write controlRecordWrite, now int64) error {
	record, table := write.record, write.table
	existing, exists, err := readControlRecord(tx.QueryRow(
		fmt.Sprintf(
			"SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM %s WHERE id = ?",
			table,
		),
		record.ID,
	), record.Domain)
	if err != nil {
		return err
	}
	if exists {
		if err := validateControlHistory(tx, existing); err != nil {
			return err
		}
	} else if err := validateNoControlHistory(tx, record.Domain, record.ID); err != nil {
		return err
	}
	from := ""
	if exists {
		from = existing.State
	}
	if write.intentJSON != nil && (!exists || existing.State != "CLAIMED" || existing.ExecutionEpoch != record.ExecutionEpoch) {
		return ErrIllegalTransition
	}
	if write.intentJSON != nil && write.allowBoundLease && string(record.Payload) != string(existing.Payload) {
		return ErrActionLeaseStale
	}
	if record.Domain == "action" && store.schema >= SchemaV5 {
		var boundEpoch uint64
		err := tx.QueryRow("SELECT epoch FROM action_leases WHERE action_id = ?", record.ID).Scan(&boundEpoch)
		if err == nil {
			if !write.allowBoundLease {
				return ErrActionLeaseRequired
			}
		} else if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
	}
	if !LegalTransition(record.Domain, from, record.State) &&
		!(write.allowBoundLease && record.Domain == "action" && store.schema >= SchemaV6 &&
			reconciliationTransition(from, record.State, existing.ExecutionEpoch, record.ExecutionEpoch)) {
		return ErrIllegalTransition
	}
	if exists {
		if record.Revision != existing.Revision+1 {
			return ErrRevisionConflict
		}
		if fencedDomains[record.Domain] {
			if err := internalprotocol.ValidateAuthoritativeEpochUpdate(
				&internalprotocol.ActionFence{ActionId: record.ID, ExecutionEpoch: record.ExecutionEpoch},
				existing.ExecutionEpoch,
			); err != nil {
				return err
			}
		}
	} else {
		if record.Revision != 1 {
			return ErrRevisionConflict
		}
		if fencedDomains[record.Domain] {
			if err := internalprotocol.ValidateFence(
				&internalprotocol.ActionFence{ActionId: record.ID, ExecutionEpoch: record.ExecutionEpoch},
			); err != nil {
				return err
			}
		}
	}
	digest, err := protocol.Digest(record)
	if err != nil {
		return err
	}
	if exists {
		result, err := tx.Exec(
			fmt.Sprintf(`UPDATE %s
			 SET revision = ?, execution_epoch = ?, state = ?, payload_json = ?,
			     record_digest = ?, writer_fencing_token = ?, updated_at = ?
			 WHERE id = ? AND revision = ?`, table),
			record.Revision,
			int64(record.ExecutionEpoch),
			record.State,
			string(record.Payload),
			digest,
			store.token,
			now,
			record.ID,
			existing.Revision,
		)
		if err != nil {
			return err
		}
		changed, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if changed != 1 {
			return ErrRevisionConflict
		}
	} else {
		if _, err := tx.Exec(
			fmt.Sprintf(`INSERT INTO %s(
				id, revision, execution_epoch, state, payload_json,
				record_digest, writer_fencing_token, updated_at
			) VALUES(?, ?, ?, ?, ?, ?, ?, ?)`, table),
			record.ID,
			record.Revision,
			int64(record.ExecutionEpoch),
			record.State,
			string(record.Payload),
			digest,
			store.token,
			now,
		); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(
		`INSERT INTO control_events(
			domain, record_id, revision, execution_epoch, state, payload_json,
			record_digest, writer_fencing_token, recorded_at
		) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		record.Domain,
		record.ID,
		record.Revision,
		int64(record.ExecutionEpoch),
		record.State,
		string(record.Payload),
		digest,
		store.token,
		now,
	); err != nil {
		return err
	}
	if write.intentJSON != nil {
		if _, err := tx.Exec(`INSERT INTO storage_dispatches(action_id,execution_epoch,operation_id,intent_json,intent_digest,claim_revision,writer_fencing_token,recorded_at)
			VALUES(?,?,?,?,?,?,?,?)`, record.ID, int64(record.ExecutionEpoch), write.operationID, string(write.intentJSON), write.intentDigest, record.Revision, store.token, now); err != nil {
			return err
		}
	}
	return nil
}

func (store *Control) Get(domain, id string) (Record, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return Record{}, false, ErrWriterFenceHeld
	}
	table, ok := tableForDomain(domain)
	if !ok {
		return Record{}, false, ErrUnknownDomain
	}
	if !ValidRecordID(id) {
		return Record{}, false, nil
	}
	if store.schema == 0 {
		return Record{}, false, nil
	}
	tx, err := store.db.Begin()
	if err != nil {
		return Record{}, false, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return Record{}, false, err
	}
	record, exists, err := readControlRecord(tx.QueryRow(
		fmt.Sprintf(
			"SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM %s WHERE id = ?",
			table,
		),
		id,
	), domain)
	if err != nil {
		return Record{}, false, err
	}
	if exists {
		if err := validateControlHistory(tx, record); err != nil {
			return Record{}, false, err
		}
	} else if err := validateNoControlHistory(tx, domain, id); err != nil {
		return Record{}, false, err
	}
	if err := tx.Commit(); err != nil {
		return Record{}, false, err
	}
	return record, exists, nil
}

func readControlRecord(row rowScanner, domain string) (Record, bool, error) {
	record, _, exists, err := scanControlRecord(row, domain)
	return record, exists, err
}

func scanControlRecord(row rowScanner, domain string, extra ...any) (Record, storedRecordMetadata, bool, error) {
	var record Record
	var executionEpoch, writerToken, updatedAt int64
	var payload, digest string
	fields := []any{
		&record.ID,
		&record.Revision,
		&executionEpoch,
		&record.State,
		&payload,
		&digest,
		&writerToken,
		&updatedAt,
	}
	err := row.Scan(append(fields, extra...)...)
	if errors.Is(err, sql.ErrNoRows) {
		return Record{}, storedRecordMetadata{}, false, nil
	}
	if err != nil {
		return Record{}, storedRecordMetadata{}, false, err
	}
	if !ValidRecordID(record.ID) || record.Revision < 1 || executionEpoch < 0 ||
		record.State == "" || writerToken < 1 || updatedAt < 0 || !isLowerSHA256(digest) {
		return Record{}, storedRecordMetadata{}, false, ErrCorruptRecord
	}
	canonical, err := canonicalControlPayload(json.RawMessage(payload))
	if err != nil || string(canonical) != payload {
		return Record{}, storedRecordMetadata{}, false, fmt.Errorf("%w: invalid canonical payload", ErrCorruptRecord)
	}
	record.Domain = domain
	record.ExecutionEpoch = uint64(executionEpoch)
	record.Payload = canonical
	expectedDigest, err := protocol.Digest(record)
	if err != nil || digest != expectedDigest {
		return Record{}, storedRecordMetadata{}, false, fmt.Errorf("%w: record digest mismatch", ErrCorruptRecord)
	}
	if !knownState(domain, record.State) {
		return Record{}, storedRecordMetadata{}, false, fmt.Errorf("%w: unknown state", ErrCorruptRecord)
	}
	return record, storedRecordMetadata{writerToken: writerToken, timestamp: updatedAt}, true, nil
}

func validateNoControlHistory(tx *sql.Tx, domain, id string) error {
	var count int
	if err := tx.QueryRow(
		"SELECT COUNT(*) FROM control_events WHERE domain = ? AND record_id = ?",
		domain,
		id,
	).Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return fmt.Errorf("%w: orphaned control events", ErrCorruptRecord)
	}
	return nil
}

func validateControlHistory(tx *sql.Tx, latest Record) error {
	boundary := int64(math.MaxInt64)
	if latest.Domain == "action" {
		var err error
		boundary, err = reconciliationHistoryBoundaryTx(tx)
		if err != nil {
			return err
		}
	}
	rows, err := tx.Query(
		`SELECT record_id, revision, execution_epoch, state, payload_json,
		        record_digest, writer_fencing_token, recorded_at, event_id
		 FROM control_events
		 WHERE domain = ? AND record_id = ?
		 ORDER BY revision`,
		latest.Domain,
		latest.ID,
	)
	if err != nil {
		return err
	}
	defer rows.Close()

	expectedRevision := int64(1)
	previousState := ""
	var previousEpoch uint64
	var previousWriterToken, previousTimestamp int64
	var final Record
	for rows.Next() {
		var eventID int64
		event, metadata, exists, err := scanControlRecord(rows, latest.Domain, &eventID)
		if err != nil {
			return err
		}
		legal := LegalTransition(latest.Domain, previousState, event.State)
		if !legal && latest.Domain == "action" && eventID > boundary &&
			reconciliationTransition(previousState, event.State, previousEpoch, event.ExecutionEpoch) {
			if err := validateReconciliationEventTx(tx, event, metadata); err != nil {
				return err
			}
			legal = true
		}
		if !exists || event.Revision != expectedRevision || !legal {
			return fmt.Errorf("%w: invalid control event sequence", ErrCorruptRecord)
		}
		if expectedRevision > 1 &&
			(metadata.writerToken < previousWriterToken || metadata.timestamp < previousTimestamp) {
			return fmt.Errorf("%w: regressing control event metadata", ErrCorruptRecord)
		}
		if fencedDomains[latest.Domain] {
			fence := &internalprotocol.ActionFence{ActionId: event.ID, ExecutionEpoch: event.ExecutionEpoch}
			if expectedRevision == 1 {
				if err := internalprotocol.ValidateFence(fence); err != nil {
					return fmt.Errorf("%w: invalid event fence", ErrCorruptRecord)
				}
			} else if err := internalprotocol.ValidateAuthoritativeEpochUpdate(fence, previousEpoch); err != nil {
				return fmt.Errorf("%w: regressing event fence", ErrCorruptRecord)
			}
		}
		final = event
		previousState = event.State
		previousEpoch = event.ExecutionEpoch
		previousWriterToken = metadata.writerToken
		previousTimestamp = metadata.timestamp
		expectedRevision++
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if err := rows.Close(); err != nil {
		return err
	}
	if expectedRevision-1 != latest.Revision || !sameControlRecord(final, latest) {
		return fmt.Errorf("%w: latest record does not match event history", ErrCorruptRecord)
	}
	table, ok := tableForDomain(latest.Domain)
	if !ok {
		return ErrCorruptRecord
	}
	var latestWriterToken, latestTimestamp int64
	if err := tx.QueryRow(
		fmt.Sprintf("SELECT writer_fencing_token, updated_at FROM %s WHERE id = ?", table),
		latest.ID,
	).Scan(&latestWriterToken, &latestTimestamp); err != nil {
		return err
	}
	if latestWriterToken != previousWriterToken || latestTimestamp != previousTimestamp {
		return fmt.Errorf("%w: latest record metadata does not match event history", ErrCorruptRecord)
	}
	return nil
}

func sameControlRecord(left, right Record) bool {
	return left.Domain == right.Domain &&
		left.ID == right.ID &&
		left.Revision == right.Revision &&
		left.ExecutionEpoch == right.ExecutionEpoch &&
		left.State == right.State &&
		string(left.Payload) == string(right.Payload)
}

func canonicalControlPayload(raw json.RawMessage) (json.RawMessage, error) {
	if raw == nil {
		raw = json.RawMessage(`{}`)
	}
	if len(raw) > maximumPayloadBytes {
		return nil, fmt.Errorf("%w: payload exceeds %d bytes", ErrInvalidPayload, maximumPayloadBytes)
	}
	var value any
	if err := decodeSingleJSON(raw, &value); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidPayload, err)
	}
	if err := rejectControlSecretMaterial(value, 0); err != nil {
		return nil, err
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidPayload, err)
	}
	if len(canonical) > maximumPayloadBytes {
		return nil, fmt.Errorf("%w: canonical payload exceeds %d bytes", ErrInvalidPayload, maximumPayloadBytes)
	}
	return json.RawMessage(canonical), nil
}

func rejectControlSecretMaterial(value any, depth int) error {
	if depth > 128 {
		return fmt.Errorf("%w: payload nesting exceeds 128", ErrInvalidPayload)
	}
	switch typed := value.(type) {
	case map[string]any:
		for key, nested := range typed {
			if secretBearingControlKey(key) {
				return ErrSecretDetected
			}
			if err := rejectControlSecretMaterial(nested, depth+1); err != nil {
				return err
			}
		}
	case []any:
		for _, nested := range typed {
			if err := rejectControlSecretMaterial(nested, depth+1); err != nil {
				return err
			}
		}
	case string:
		lower := strings.ToLower(typed)
		if strings.Contains(lower, "age-secret-key-") ||
			strings.Contains(lower, "-----begin") && strings.Contains(lower, "private key") {
			return ErrSecretDetected
		}
	}
	return nil
}

func secretBearingControlKey(key string) bool {
	var normalized strings.Builder
	for _, character := range strings.ToLower(key) {
		if character >= 'a' && character <= 'z' || character >= '0' && character <= '9' {
			normalized.WriteRune(character)
		}
	}
	name := normalized.String()
	if name == "fencingtoken" {
		return false
	}
	for _, safeSuffix := range []string{"digest", "reference", "ref", "id", "type", "provider"} {
		if strings.HasSuffix(name, safeSuffix) {
			return false
		}
	}
	for _, fragment := range []string{
		"password", "passwd", "privatekey", "ageidentity", "apikey",
		"accesskey", "secretkey", "token", "credential", "oauth", "bearer",
	} {
		if strings.Contains(name, fragment) {
			return true
		}
	}
	return strings.HasSuffix(name, "secret")
}

func knownState(domain, state string) bool {
	if domain == "action" && state == "RECONCILING" {
		return true
	}
	edges, ok := transitions[domain]
	if !ok {
		return false
	}
	for from, next := range edges {
		if from == state {
			return true
		}
		for _, candidate := range next {
			if candidate == state {
				return true
			}
		}
	}
	return false
}

func (store *Control) assertWriterTx(tx *sql.Tx, now int64) (int64, error) {
	if store.closed {
		return 0, ErrWriterFenceHeld
	}
	leaseUntil, err := store.nextLeaseUntil(now)
	if err != nil {
		return 0, err
	}
	var runtimeName, mode, owner string
	var token, currentLeaseUntil int64
	if err := tx.QueryRow(
		"SELECT runtime, mode, owner_instance_id, fencing_token, lease_until FROM control_writer WHERE singleton = 1",
	).Scan(&runtimeName, &mode, &owner, &token, &currentLeaseUntil); err != nil {
		return 0, fmt.Errorf("%w: writer row unavailable", ErrWriterFenceHeld)
	}
	if runtimeName != RuntimeGo || mode != ModeShadow || owner != store.owner ||
		token != store.token || currentLeaseUntil < 0 || now >= currentLeaseUntil {
		return 0, ErrWriterFenceHeld
	}
	result, err := tx.Exec(
		`UPDATE control_writer SET lease_until = ?
		 WHERE singleton = 1 AND runtime = ? AND mode = ?
		   AND owner_instance_id = ? AND fencing_token = ? AND lease_until > ?`,
		leaseUntil,
		RuntimeGo,
		ModeShadow,
		store.owner,
		store.token,
		now,
	)
	if err != nil {
		return 0, err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return 0, err
	}
	if changed != 1 {
		return 0, ErrWriterFenceHeld
	}
	return leaseUntil, nil
}

func (store *Control) nextLeaseUntil(now int64) (int64, error) {
	if now < 0 {
		return 0, ErrWriterFenceHeld
	}
	if store.leaseSeconds > math.MaxInt64-now {
		return math.MaxInt64, nil
	}
	return now + store.leaseSeconds, nil
}

func (store *Control) MutateProduction(_ string, _ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func (store *Control) ExportSnapshot() (Snapshot, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return Snapshot{}, ErrWriterFenceHeld
	}
	snapshot := Snapshot{
		SchemaVersion: store.schema,
		Runtime:       RuntimeGo,
		Mode:          ModeShadow,
		Writer: WriterLease{
			Runtime:         RuntimeGo,
			Mode:            ModeShadow,
			OwnerInstanceID: store.owner,
			FencingToken:    store.token,
			LeaseUntil:      store.leaseUntil,
		},
	}
	if store.schema >= SchemaV1 {
		tx, err := store.db.Begin()
		if err != nil {
			return Snapshot{}, err
		}
		defer tx.Rollback()
		if err := verifySchemaTx(tx, store.schema); err != nil {
			return Snapshot{}, err
		}
		for _, domain := range controlDomainOrder {
			table, _ := tableForDomain(domain)
			rows, err := tx.Query(fmt.Sprintf(
				"SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM %s",
				table,
			))
			if err != nil {
				return Snapshot{}, err
			}
			for rows.Next() {
				record, ok, err := readControlRecord(rows, domain)
				if err != nil {
					_ = rows.Close()
					return Snapshot{}, err
				}
				if ok {
					snapshot.Records = append(snapshot.Records, record)
				}
			}
			if err := rows.Err(); err != nil {
				_ = rows.Close()
				return Snapshot{}, err
			}
			if err := rows.Close(); err != nil {
				return Snapshot{}, err
			}
		}
		for _, record := range snapshot.Records {
			if err := validateControlHistory(tx, record); err != nil {
				return Snapshot{}, err
			}
		}
		if err := tx.Commit(); err != nil {
			return Snapshot{}, err
		}
	}
	sort.Slice(snapshot.Records, func(left, right int) bool {
		if snapshot.Records[left].Domain != snapshot.Records[right].Domain {
			return snapshot.Records[left].Domain < snapshot.Records[right].Domain
		}
		return snapshot.Records[left].ID < snapshot.Records[right].ID
	})
	digest, err := protocol.Digest(map[string]any{
		"schemaVersion": snapshot.SchemaVersion,
		"runtime":       snapshot.Runtime,
		"mode":          snapshot.Mode,
		"records":       snapshot.Records,
	})
	if err != nil {
		return Snapshot{}, err
	}
	snapshot.Digest = digest
	return snapshot, nil
}

func (store *Control) Rollback(version int) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return ErrWriterFenceHeld
	}
	if version != 0 {
		return ErrSchemaInactive
	}
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := store.now()
	leaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return err
	}
	if store.schema >= SchemaV5 {
		if err := retireEmptyActionAdmissionTx(tx); err != nil {
			return err
		}
	}
	if store.schema >= SchemaV6 {
		if _, err := tx.Exec("DROP TABLE action_reconciliation_boundary"); err != nil {
			return err
		}
	}
	if store.schema >= SchemaV4 {
		if err := retireEmptyStorageDispatchesTx(tx); err != nil {
			return err
		}
	}
	if _, err := tx.Exec("DROP TABLE IF EXISTS control_operations"); err != nil {
		return err
	}
	if _, err := tx.Exec("DROP TABLE IF EXISTS control_cutover_events"); err != nil {
		return err
	}
	if _, err := tx.Exec("DROP TABLE IF EXISTS control_cutover"); err != nil {
		return err
	}
	if _, err := tx.Exec("DROP TABLE IF EXISTS control_events"); err != nil {
		return err
	}
	for _, table := range controlTableNames {
		if _, err := tx.Exec("DROP TABLE IF EXISTS " + table); err != nil {
			return err
		}
	}
	if _, err := tx.Exec("DELETE FROM schema_migrations"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version = 0 WHERE singleton = 1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version = 0"); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	store.schema = 0
	store.leaseUntil = leaseUntil
	return nil
}
