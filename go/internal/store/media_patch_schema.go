package store

import (
	"database/sql"
	"fmt"
	"strings"
)

// Only operation identity/admission and signed result witnesses live here.
// Media library bytes and publication recovery remain exclusively in Rust.
var mediaPatchSchemaObjects = map[string]string{
	"control_media_operations": `CREATE TABLE control_media_operations (
        operation_id TEXT PRIMARY KEY, intent BLOB NOT NULL CHECK(length(intent)>0 AND length(intent)<=2097152),
        request_sha256 BLOB NOT NULL CHECK(length(request_sha256)=32),
        action_id TEXT NOT NULL UNIQUE, epoch INTEGER NOT NULL CHECK(epoch>0),
        owner TEXT NOT NULL, claim_token TEXT NOT NULL, writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>0),
        lease_until INTEGER NOT NULL CHECK(lease_until>=0), fleet_id TEXT NOT NULL, environment TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('CLAIMED','DISPATCHED','EFFECT_UNKNOWN','APPLIED','REFUSED','CANCELLED')),
        preparation_sha256 BLOB, outcome BLOB,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        CHECK((state='CLAIMED' AND preparation_sha256 IS NULL AND outcome IS NULL)
           OR (state IN ('DISPATCHED','EFFECT_UNKNOWN') AND length(preparation_sha256)=32 AND outcome IS NULL)
           OR (state='APPLIED' AND length(preparation_sha256)=32 AND length(outcome)>0 AND length(outcome)<=2097152)
           OR (state='REFUSED' AND (preparation_sha256 IS NULL OR length(preparation_sha256)=32) AND length(outcome)>0 AND length(outcome)<=2097152)
           OR (state='CANCELLED' AND preparation_sha256 IS NULL AND outcome IS NULL))
    ) STRICT`,
	"control_media_one_publication": `CREATE UNIQUE INDEX control_media_one_publication ON control_media_operations((1)) WHERE state IN ('CLAIMED','DISPATCHED','EFFECT_UNKNOWN')`,
	"control_media_no_delete":       `CREATE TRIGGER control_media_no_delete BEFORE DELETE ON control_media_operations BEGIN SELECT RAISE(ABORT,'MEDIA_HISTORY_IMMUTABLE'); END`,
	"control_media_no_replace":      `CREATE TRIGGER control_media_no_replace BEFORE INSERT ON control_media_operations WHEN EXISTS(SELECT 1 FROM control_media_operations WHERE operation_id=NEW.operation_id OR action_id=NEW.action_id OR rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'MEDIA_HISTORY_IMMUTABLE'); END`,
	"control_media_identity":        `CREATE TRIGGER control_media_identity BEFORE UPDATE ON control_media_operations WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.intent IS NOT OLD.intent OR NEW.request_sha256 IS NOT OLD.request_sha256 OR NEW.action_id IS NOT OLD.action_id OR NEW.epoch IS NOT OLD.epoch OR NEW.owner IS NOT OLD.owner OR NEW.claim_token IS NOT OLD.claim_token OR NEW.writer_fencing_token IS NOT OLD.writer_fencing_token OR NEW.lease_until IS NOT OLD.lease_until OR NEW.fleet_id IS NOT OLD.fleet_id OR NEW.environment IS NOT OLD.environment OR NEW.created_at IS NOT OLD.created_at OR (OLD.preparation_sha256 IS NOT NULL AND NEW.preparation_sha256 IS NOT OLD.preparation_sha256) BEGIN SELECT RAISE(ABORT,'MEDIA_IDENTITY_IMMUTABLE'); END`,
	"control_media_transition":      `CREATE TRIGGER control_media_transition BEFORE UPDATE ON control_media_operations WHEN NOT ((OLD.state='CLAIMED' AND NEW.state IN ('DISPATCHED','CANCELLED','REFUSED')) OR (OLD.state='DISPATCHED' AND NEW.state IN ('EFFECT_UNKNOWN','APPLIED','REFUSED')) OR (OLD.state='EFFECT_UNKNOWN' AND NEW.state IN ('APPLIED','REFUSED'))) BEGIN SELECT RAISE(ABORT,'MEDIA_TRANSITION_DENIED'); END`,
}
var mediaPatchSchemaOrder = []string{"control_media_operations", "control_media_one_publication", "control_media_no_delete", "control_media_no_replace", "control_media_identity", "control_media_transition"}

func (store *Control) migrateToV19Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV18); err != nil {
		return err
	}
	statements := []string{
		`CREATE TABLE control_store_meta_v19 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 19),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v19 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v19 RENAME TO control_store_meta`,
	}
	for _, name := range mediaPatchSchemaOrder {
		statements = append(statements, mediaPatchSchemaObjects[name])
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
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(19,?,?)", now, "admit typed media operations without transferring media data ownership to Go"); err != nil {
		return err
	}
	for _, q := range []string{"UPDATE control_store_meta SET schema_version=19 WHERE singleton=1", "PRAGMA user_version=19"} {
		if _, err := tx.Exec(q); err != nil {
			return err
		}
	}
	store.schema = SchemaV19
	return verifyMediaPatchSchemaTx(tx)
}
func verifyMediaPatchSchemaTx(tx *sql.Tx) error {
	for name, expected := range mediaPatchSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: media schema %s", ErrForeignRuntimeStore, name)
		}
	}
	return nil
}
func retireEmptyMediaPatchTx(tx *sql.Tx) error {
	var n int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_media_operations").Scan(&n); err != nil {
		return err
	}
	if n != 0 {
		return ErrAdmissionHistoryRetained
	}
	for i := len(mediaPatchSchemaOrder) - 1; i >= 0; i-- {
		name := mediaPatchSchemaOrder[i]
		kind := "TRIGGER"
		if name == "control_media_operations" {
			kind = "TABLE"
		}
		if name == "control_media_one_publication" {
			kind = "INDEX"
		}
		if _, err := tx.Exec("DROP " + kind + " " + name); err != nil {
			return err
		}
	}
	return nil
}
