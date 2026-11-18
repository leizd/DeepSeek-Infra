package store

import (
	"database/sql"
	"fmt"
)

const targetMarkerTable = "control_target_marker_operations"
const targetMarkerPending = "EXISTS(SELECT 1 FROM control_target_marker_operations WHERE state IN ('CLAIMED','DISPATCHED'))"

var targetMarkerSchema = []struct{ name, kind, statement string }{
	{targetMarkerTable, "table", `CREATE TABLE control_target_marker_operations (
		operation_id TEXT NOT NULL, execution_epoch INTEGER NOT NULL CHECK(execution_epoch>0),
		actor TEXT NOT NULL, claim BLOB NOT NULL CHECK(length(claim)>0 AND length(claim)<=98304),
		claim_sha256 BLOB NOT NULL CHECK(length(claim_sha256)=32),
		state TEXT NOT NULL, preparation_sha256 BLOB, receipt BLOB,
		PRIMARY KEY(operation_id,execution_epoch),
		CHECK ((state IN ('CLAIMED','ABORTED') AND preparation_sha256 IS NULL AND receipt IS NULL)
			OR (state='DISPATCHED' AND preparation_sha256 IS NOT NULL AND length(preparation_sha256)=32 AND receipt IS NULL)
			OR (state='SETTLED' AND preparation_sha256 IS NOT NULL AND length(preparation_sha256)=32
				AND receipt IS NOT NULL AND length(receipt)>0 AND length(receipt)<=65536))
	) STRICT`},
	{"control_target_marker_single_pending", "index", "CREATE UNIQUE INDEX control_target_marker_single_pending ON control_target_marker_operations(1) WHERE state IN ('CLAIMED','DISPATCHED')"},
	{"control_target_marker_advance", "trigger", `CREATE TRIGGER control_target_marker_advance BEFORE UPDATE ON control_target_marker_operations
		WHEN NEW.operation_id<>OLD.operation_id OR NEW.execution_epoch<>OLD.execution_epoch OR NEW.actor<>OLD.actor
			OR NEW.claim<>OLD.claim OR NEW.claim_sha256<>OLD.claim_sha256
			OR NOT ((OLD.state='CLAIMED' AND NEW.state IN ('DISPATCHED','ABORTED'))
				OR (OLD.state='DISPATCHED' AND NEW.state='SETTLED' AND NEW.preparation_sha256=OLD.preparation_sha256))
		BEGIN SELECT RAISE(ABORT,'BACKUP_TARGET_MARKER_HISTORY_IMMUTABLE'); END`},
	{"control_target_marker_insert", "trigger", `CREATE TRIGGER control_target_marker_insert BEFORE INSERT ON control_target_marker_operations
		WHEN NEW.state<>'CLAIMED' OR NEW.execution_epoch<>1+COALESCE(
			(SELECT MAX(execution_epoch) FROM control_target_marker_operations WHERE operation_id=NEW.operation_id),0)
			OR EXISTS(SELECT 1 FROM control_target_marker_operations WHERE rowid=NEW.rowid)
		OR EXISTS(SELECT 1 FROM control_target_marker_operations WHERE operation_id=NEW.operation_id AND state<>'ABORTED')
		OR ` + targetMarkerPending + `
		BEGIN SELECT RAISE(ABORT,'BACKUP_TARGET_MARKER_HISTORY_IMMUTABLE'); END`},
	{"control_target_marker_no_delete", "trigger", "CREATE TRIGGER control_target_marker_no_delete BEFORE DELETE ON control_target_marker_operations BEGIN SELECT RAISE(ABORT,'BACKUP_TARGET_MARKER_HISTORY_IMMUTABLE'); END"},
	{"control_target_guard_insert", "trigger", "CREATE TRIGGER control_target_guard_insert BEFORE INSERT ON targets WHEN " + targetMarkerPending + " BEGIN SELECT RAISE(ABORT,'BACKUP_TARGET_MARKER_EFFECT_PENDING'); END"},
	{"control_target_guard_update", "trigger", "CREATE TRIGGER control_target_guard_update BEFORE UPDATE ON targets WHEN " + targetMarkerPending + " BEGIN SELECT RAISE(ABORT,'BACKUP_TARGET_MARKER_EFFECT_PENDING'); END"},
	{"control_target_guard_delete", "trigger", "CREATE TRIGGER control_target_guard_delete BEFORE DELETE ON targets WHEN " + targetMarkerPending + " BEGIN SELECT RAISE(ABORT,'BACKUP_TARGET_MARKER_EFFECT_PENDING'); END"},
	{"control_target_marker_cutover", "trigger", "CREATE TRIGGER control_target_marker_cutover BEFORE UPDATE ON control_cutover WHEN OLD.domain='target' AND " + targetMarkerPending + " BEGIN SELECT RAISE(ABORT,'BACKUP_TARGET_MARKER_EFFECT_PENDING'); END"},
}

func (store *Control) migrateToV23Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV22); err != nil {
		return err
	}
	statements := []string{
		"CREATE TABLE control_store_meta_v23 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 23),unique_writer TEXT NOT NULL) STRICT",
		"INSERT INTO control_store_meta_v23 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_store_meta_v23 RENAME TO control_store_meta",
	}
	for _, object := range targetMarkerSchema {
		statements = append(statements, object.statement)
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
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(23,?,?)", now, "reserve and reconcile filesystem target marker effects"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=23 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=23"); err != nil {
		return err
	}
	store.schema = SchemaV23
	return verifySchemaTx(tx, SchemaV23)
}

type targetMarkerSchemaEditor interface {
	QueryRow(string, ...any) *sql.Row
	Exec(string, ...any) (sql.Result, error)
}

func verifyTargetMarkerSchemaTx(tx targetMarkerSchemaEditor) error {
	for _, object := range targetMarkerSchema {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=? AND type=?", object.name, object.kind).Scan(&actual); err != nil || actual != object.statement {
			return fmt.Errorf("%w: target marker object %s", ErrForeignRuntimeStore, object.name)
		}
	}
	return nil
}

func retireEmptyTargetMarkersTx(tx targetMarkerSchemaEditor) error {
	if err := verifyTargetMarkerSchemaTx(tx); err != nil {
		return err
	}
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_target_marker_operations").Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrSchemaInactive
	}
	for i := len(targetMarkerSchema) - 1; i >= 0; i-- {
		object := targetMarkerSchema[i]
		if _, err := tx.Exec("DROP " + object.kind + " " + object.name); err != nil {
			return err
		}
	}
	return nil
}
