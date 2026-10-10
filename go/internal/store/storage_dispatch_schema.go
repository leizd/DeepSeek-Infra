package store

import (
	"database/sql"
	"errors"
	"fmt"
	"strings"
)

var ErrDispatchHistoryRetained = errors.New("DISPATCH_HISTORY_RETAINED")

// Only Go may access this journal. Parentage refers to the retained Go dispatch
// event, never the Rust database or an inferred remote effect.
var storageDispatchSchemaObjects = map[string]string{
	"storage_dispatches": `CREATE TABLE storage_dispatches (
		action_id TEXT NOT NULL CHECK(length(action_id)>0),
		execution_epoch INTEGER NOT NULL CHECK(execution_epoch>0),
		operation_id TEXT NOT NULL CHECK(length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 1024 AND instr(operation_id,char(0))=0),
		intent_json TEXT NOT NULL CHECK(json_valid(intent_json)),
		intent_digest TEXT NOT NULL CHECK(length(intent_digest)=64 AND intent_digest=lower(intent_digest)),
		claim_revision INTEGER NOT NULL CHECK(claim_revision>1),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>0),
		recorded_at INTEGER NOT NULL CHECK(recorded_at>=0),
		PRIMARY KEY(action_id,execution_epoch)
	) STRICT`,
	"storage_dispatches_parent": `CREATE TRIGGER storage_dispatches_parent BEFORE INSERT ON storage_dispatches
	WHEN NOT EXISTS (SELECT 1 FROM control_events e JOIN action_journal a ON a.id=e.record_id
		WHERE e.domain='action' AND e.record_id=NEW.action_id AND e.execution_epoch=NEW.execution_epoch
		AND e.revision=NEW.claim_revision AND e.state='EXECUTING' AND e.writer_fencing_token=NEW.writer_fencing_token
		AND a.execution_epoch=e.execution_epoch AND a.revision=e.revision AND a.state=e.state)
	BEGIN SELECT RAISE(ABORT,'DISPATCH_REQUIRES_CLAIM'); END`,
	"storage_dispatches_no_update": `CREATE TRIGGER storage_dispatches_no_update BEFORE UPDATE ON storage_dispatches
	BEGIN SELECT RAISE(ABORT,'STORAGE_DISPATCH_IMMUTABLE'); END`,
	"storage_dispatches_no_delete": `CREATE TRIGGER storage_dispatches_no_delete BEFORE DELETE ON storage_dispatches
	BEGIN SELECT RAISE(ABORT,'STORAGE_DISPATCH_IMMUTABLE'); END`,
	"storage_dispatches_no_replace": `CREATE TRIGGER storage_dispatches_no_replace BEFORE INSERT ON storage_dispatches
	WHEN EXISTS (SELECT 1 FROM storage_dispatches d WHERE d.rowid=NEW.rowid OR (d.action_id=NEW.action_id AND d.execution_epoch=NEW.execution_epoch))
	BEGIN SELECT RAISE(ABORT,'STORAGE_DISPATCH_IMMUTABLE'); END`,
}

func (store *Control) migrateToV4Tx(tx *sql.Tx) error {
	// SQLite's create/copy/drop/rename procedure changes only the version ceiling
	// on the identity row. No history table is rebuilt or backfilled.
	// https://www.sqlite.org/lang_altertable.html#otheralter
	statements := []string{
		`CREATE TABLE control_store_meta_v4 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 4),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v4 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v4 RENAME TO control_store_meta`,
		storageDispatchSchemaObjects["storage_dispatches"],
	}
	for _, name := range []string{"storage_dispatches_parent", "storage_dispatches_no_update", "storage_dispatches_no_delete", "storage_dispatches_no_replace"} {
		statements = append(statements, storageDispatchSchemaObjects[name])
	}
	for _, statement := range statements {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations(version,applied_at,description) VALUES(4,?,?)", now, "bind storage dispatch intent to action claim"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=4 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=4"); err != nil {
		return err
	}
	store.schema = SchemaV4
	return nil
}

func verifyStorageDispatchSchemaTx(tx *sql.Tx) error {
	for name, expected := range storageDispatchSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: storage dispatch schema %s", ErrForeignRuntimeStore, name)
		}
	}
	return nil
}

func retireEmptyStorageDispatchesTx(tx *sql.Tx) error {
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM storage_dispatches").Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrDispatchHistoryRetained
	}
	_, err := tx.Exec("DROP TABLE storage_dispatches")
	return err
}
