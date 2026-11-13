package store

import (
	"database/sql"
	"fmt"
	"strings"
)

var targetLocationSchemaObjects = map[string]string{
	"control_target_location_boundary": `CREATE TABLE control_target_location_boundary (singleton INTEGER PRIMARY KEY CHECK(singleton=1), last_legacy_event_id INTEGER NOT NULL CHECK(last_legacy_event_id>=0)) STRICT`,
	"control_target_registration_locations": `CREATE TABLE control_target_registration_locations (
        operation_id TEXT PRIMARY KEY, previous_path TEXT NOT NULL CHECK(length(previous_path)>0),
        observation BLOB NOT NULL CHECK(length(observation)>0 AND length(observation)<=131072),
        FOREIGN KEY(operation_id) REFERENCES control_target_registrations(operation_id)
    ) STRICT`,
}

var targetLocationSchemaOrder = []string{
	"control_target_location_boundary", "control_target_registration_locations",
	"control_target_location_boundary_no_update", "control_target_location_boundary_no_delete", "control_target_location_boundary_no_replace",
	"control_target_registration_locations_no_update", "control_target_registration_locations_no_delete", "control_target_registration_locations_no_replace",
}

func init() {
	for _, table := range targetLocationSchemaOrder[:2] {
		for _, operation := range []string{"update", "delete"} {
			name := table + "_no_" + operation
			targetLocationSchemaObjects[name] = "CREATE TRIGGER " + name + " BEFORE " + strings.ToUpper(operation) + " ON " + table + " BEGIN SELECT RAISE(ABORT,'TARGET_LOCATION_HISTORY_IMMUTABLE'); END"
		}
		name := table + "_no_replace"
		key := "operation_id=NEW.operation_id OR rowid=NEW.rowid"
		if table == "control_target_location_boundary" {
			key = "singleton=NEW.singleton OR rowid=NEW.rowid"
		}
		targetLocationSchemaObjects[name] = "CREATE TRIGGER " + name + " BEFORE INSERT ON " + table + " WHEN EXISTS(SELECT 1 FROM " + table + " WHERE " + key + ") BEGIN SELECT RAISE(ABORT,'TARGET_LOCATION_HISTORY_IMMUTABLE'); END"
	}
}

func (store *Control) migrateToV22Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV21); err != nil {
		return err
	}
	statements := []string{
		`CREATE TABLE control_store_meta_v22 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 22),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v22 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v22 RENAME TO control_store_meta`,
	}
	for _, name := range targetLocationSchemaOrder {
		statements = append(statements, targetLocationSchemaObjects[name])
	}
	for _, statement := range statements {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	if _, err := tx.Exec("INSERT INTO control_target_location_boundary SELECT 1,COALESCE(MAX(event_id),0) FROM control_events"); err != nil {
		return err
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(22,?,?)", now, "retain Rust location witnesses for target relocation without reinterpreting legacy registration history"); err != nil {
		return err
	}
	for _, statement := range []string{"UPDATE control_store_meta SET schema_version=22 WHERE singleton=1", "PRAGMA user_version=22"} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	store.schema = SchemaV22
	return verifySchemaTx(tx, SchemaV22)
}

func verifyTargetLocationSchemaTx(tx *sql.Tx) error {
	for name, expected := range targetLocationSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: target location schema %s", ErrForeignRuntimeStore, name)
		}
	}
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_target_location_boundary WHERE singleton=1").Scan(&count); err != nil || count != 1 {
		return ErrForeignRuntimeStore
	}
	if err := tx.QueryRow(`SELECT COUNT(*) FROM control_target_registration_locations l LEFT JOIN control_target_registrations r ON r.operation_id=l.operation_id WHERE r.operation_id IS NULL`).Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrCorruptRecord
	}
	return nil
}

func retireEmptyTargetLocationsTx(tx *sql.Tx) error {
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_target_registration_locations").Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrAdmissionHistoryRetained
	}
	for i := len(targetLocationSchemaOrder) - 1; i >= 0; i-- {
		kind := "TRIGGER"
		if i < 2 {
			kind = "TABLE"
		}
		if _, err := tx.Exec("DROP " + kind + " " + targetLocationSchemaOrder[i]); err != nil {
			return err
		}
	}
	return nil
}
