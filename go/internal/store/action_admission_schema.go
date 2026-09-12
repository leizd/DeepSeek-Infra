package store

import (
	"database/sql"
	"fmt"
	"strings"
)

var actionAdmissionSchemaObjects = map[string]string{
	"action_leases": `CREATE TABLE action_leases (
		action_id TEXT PRIMARY KEY CHECK(length(action_id)>0),
		owner TEXT NOT NULL CHECK(length(owner)>0),
		epoch INTEGER NOT NULL CHECK(epoch>0),
		claim_token TEXT NOT NULL CHECK(length(claim_token)>=16 AND instr(claim_token,char(0))=0),
		lease_until INTEGER NOT NULL CHECK(lease_until>0),
		acquired_at INTEGER NOT NULL CHECK(acquired_at>=0),
		updated_at INTEGER NOT NULL CHECK(updated_at>=acquired_at),
		claim_revision INTEGER NOT NULL CHECK(claim_revision>0),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>0),
		terminal_state TEXT CHECK(terminal_state IS NULL OR terminal_state IN ('SUCCEEDED','FAILED_BEFORE_EFFECT'))
	) STRICT`,
	"action_lease_events": `CREATE TABLE action_lease_events (
		event_id INTEGER PRIMARY KEY AUTOINCREMENT,
		action_id TEXT NOT NULL CHECK(length(action_id)>0),
		event_type TEXT NOT NULL CHECK(event_type IN ('ADMITTED','RENEWED','TAKEOVER','TERMINATED')),
		owner TEXT NOT NULL CHECK(length(owner)>0),
		epoch INTEGER NOT NULL CHECK(epoch>0),
		claim_token TEXT NOT NULL CHECK(length(claim_token)>=16 AND instr(claim_token,char(0))=0),
		lease_until INTEGER NOT NULL CHECK(lease_until>0),
		claim_revision INTEGER NOT NULL CHECK(claim_revision>0),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>0),
		recorded_at INTEGER NOT NULL CHECK(recorded_at>=0),
		resource_keys_json TEXT NOT NULL DEFAULT '[]' CHECK(length(CAST(resource_keys_json AS BLOB))<=1048576 AND json_valid(resource_keys_json) AND json_type(resource_keys_json)='array')
	) STRICT`,
	"action_lease_events_no_update": `CREATE TRIGGER action_lease_events_no_update BEFORE UPDATE ON action_lease_events
	BEGIN SELECT RAISE(ABORT,'ACTION_LEASE_EVENT_IMMUTABLE'); END`,
	"action_lease_events_no_delete": `CREATE TRIGGER action_lease_events_no_delete BEFORE DELETE ON action_lease_events
	BEGIN SELECT RAISE(ABORT,'ACTION_LEASE_EVENT_IMMUTABLE'); END`,
	"action_lease_events_no_replace": `CREATE TRIGGER action_lease_events_no_replace BEFORE INSERT ON action_lease_events
	WHEN EXISTS(SELECT 1 FROM action_lease_events WHERE event_id=NEW.event_id)
	OR (NEW.event_type IN ('ADMITTED','TAKEOVER') AND EXISTS(SELECT 1 FROM action_lease_events
		WHERE action_id=NEW.action_id AND epoch=NEW.epoch AND event_type IN ('ADMITTED','TAKEOVER')))
	BEGIN SELECT RAISE(ABORT,'ACTION_LEASE_EVENT_IMMUTABLE'); END`,
	"action_resource_leases": `CREATE TABLE action_resource_leases (
		resource_key TEXT PRIMARY KEY CHECK(length(resource_key)>0 AND length(CAST(resource_key AS BLOB))<=1024 AND instr(resource_key,char(0))=0),
		action_id TEXT NOT NULL CHECK(length(action_id)>0),
		owner TEXT NOT NULL CHECK(length(owner)>0),
		epoch INTEGER NOT NULL CHECK(epoch>0),
		acquired_at INTEGER NOT NULL CHECK(acquired_at>=0),
		lease_until INTEGER NOT NULL CHECK(lease_until>0),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>0)
	) STRICT`,
	"idx_action_resource_leases_action": `CREATE INDEX idx_action_resource_leases_action ON action_resource_leases(action_id)`,
}

func (store *Control) migrateToV5Tx(tx *sql.Tx) error {
	statements := []string{
		`CREATE TABLE control_store_meta_v5 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 5),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v5 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v5 RENAME TO control_store_meta`,
		actionAdmissionSchemaObjects["action_leases"],
		actionAdmissionSchemaObjects["action_lease_events"],
		actionAdmissionSchemaObjects["action_lease_events_no_update"],
		actionAdmissionSchemaObjects["action_lease_events_no_delete"],
		actionAdmissionSchemaObjects["action_lease_events_no_replace"],
		actionAdmissionSchemaObjects["action_resource_leases"],
		actionAdmissionSchemaObjects["idx_action_resource_leases_action"],
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
	if _, err := tx.Exec("INSERT INTO schema_migrations(version,applied_at,description) VALUES(5,?,?)", now, "action leases and atomic resource admission"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=5 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=5"); err != nil {
		return err
	}
	store.schema = SchemaV5
	return nil
}

func verifyActionAdmissionSchemaTx(tx *sql.Tx) error {
	for name, expected := range actionAdmissionSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: action admission schema %s", ErrForeignRuntimeStore, name)
		}
	}
	return nil
}

func retireEmptyActionAdmissionTx(tx *sql.Tx) error {
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM action_leases").Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrAdmissionHistoryRetained
	}
	if err := tx.QueryRow("SELECT COUNT(*) FROM action_lease_events").Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrAdmissionHistoryRetained
	}
	if err := tx.QueryRow("SELECT COUNT(*) FROM action_resource_leases").Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrAdmissionHistoryRetained
	}
	for _, table := range []string{"action_resource_leases", "action_lease_events", "action_leases"} {
		if _, err := tx.Exec("DROP TABLE " + table); err != nil {
			return err
		}
	}
	return nil
}
