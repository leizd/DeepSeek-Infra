package store

import (
	"database/sql"
	"fmt"
	"strings"
)

var targetRegistrationSchemaObjects = map[string]string{
	"control_target_registration_boundary": `CREATE TABLE control_target_registration_boundary (singleton INTEGER PRIMARY KEY CHECK(singleton=1), last_legacy_event_id INTEGER NOT NULL CHECK(last_legacy_event_id>=0)) STRICT`,
	"control_target_registration_identity": `CREATE TABLE control_target_registration_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), signer_public_key TEXT NOT NULL, fleet_id TEXT NOT NULL, environment TEXT NOT NULL) STRICT`,
	"control_target_registrations": `CREATE TABLE control_target_registrations (
        operation_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        public_generation INTEGER NOT NULL CHECK(public_generation>0),
        observation BLOB NOT NULL CHECK(length(observation)>0 AND length(observation)<=4325376),
        UNIQUE(target_id,revision)
    ) STRICT`,
}

func init() {
	for _, table := range []string{"control_target_registration_identity", "control_target_registrations", "control_target_registration_boundary"} {
		for _, action := range []string{"update", "delete"} {
			name := table + "_no_" + action
			targetRegistrationSchemaObjects[name] = "CREATE TRIGGER " + name + " BEFORE " + strings.ToUpper(action) + " ON " + table + " BEGIN SELECT RAISE(ABORT,'TARGET_REGISTRATION_HISTORY_IMMUTABLE'); END"
		}
		name := table + "_no_replace"
		key := "operation_id=NEW.operation_id OR rowid=NEW.rowid"
		if table != "control_target_registrations" {
			key = "singleton=NEW.singleton OR rowid=NEW.rowid"
		}
		targetRegistrationSchemaObjects[name] = "CREATE TRIGGER " + name + " BEFORE INSERT ON " + table + " WHEN EXISTS(SELECT 1 FROM " + table + " WHERE " + key + ") BEGIN SELECT RAISE(ABORT,'TARGET_REGISTRATION_HISTORY_IMMUTABLE'); END"
	}
}

var targetRegistrationSchemaOrder = []string{
	"control_target_registration_identity", "control_target_registrations", "control_target_registration_boundary",
	"control_target_registration_identity_no_update", "control_target_registration_identity_no_delete", "control_target_registration_identity_no_replace",
	"control_target_registrations_no_update", "control_target_registrations_no_delete", "control_target_registrations_no_replace",
	"control_target_registration_boundary_no_update", "control_target_registration_boundary_no_delete", "control_target_registration_boundary_no_replace",
}

func (store *Control) migrateToV21Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV20); err != nil {
		return err
	}
	statements := []string{
		`CREATE TABLE control_store_meta_v21 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 21),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v21 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v21 RENAME TO control_store_meta`,
	}
	for _, name := range targetRegistrationSchemaOrder {
		statements = append(statements, targetRegistrationSchemaObjects[name])
	}
	for _, statement := range statements {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	if _, err := tx.Exec("INSERT INTO control_target_registration_boundary SELECT 1,COALESCE(MAX(event_id),0) FROM control_events"); err != nil {
		return err
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(21,?,?)", now, "retain target registration witnesses and public generations independently of monotonic control history"); err != nil {
		return err
	}
	for _, statement := range []string{"UPDATE control_store_meta SET schema_version=21 WHERE singleton=1", "PRAGMA user_version=21"} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	store.schema = SchemaV21
	return verifySchemaTx(tx, SchemaV21)
}

func verifyTargetRegistrationSchemaTx(tx *sql.Tx) error {
	for name, expected := range targetRegistrationSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: target registration schema %s", ErrForeignRuntimeStore, name)
		}
	}
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_target_registration_boundary WHERE singleton=1").Scan(&count); err != nil || count != 1 {
		return ErrForeignRuntimeStore
	}
	return nil
}

func (store *Control) verifyTargetRegistrationIdentityTx(tx *sql.Tx) error {
	if store.schema < SchemaV21 {
		return nil
	}
	var key, fleet, environment string
	err := tx.QueryRow("SELECT signer_public_key,fleet_id,environment FROM control_target_registration_identity WHERE singleton=1").Scan(&key, &fleet, &environment)
	if err == sql.ErrNoRows {
		var count int
		if err := tx.QueryRow("SELECT COUNT(*) FROM control_target_registrations").Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return ErrCorruptRecord
		}
		return nil
	}
	if err != nil || key == "" || key != store.promotionSignerKey || fleet != store.fleetID || environment != store.environment {
		return ErrForeignRuntimeStore
	}
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_target_registrations").Scan(&count); err != nil {
		return err
	}
	if count == 0 {
		return ErrCorruptRecord
	}
	return nil
}

func retireEmptyTargetRegistrationsTx(tx *sql.Tx) error {
	for _, name := range targetRegistrationSchemaOrder[:2] {
		var count int
		if err := tx.QueryRow("SELECT COUNT(*) FROM " + name).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return ErrAdmissionHistoryRetained
		}
	}
	for i := len(targetRegistrationSchemaOrder) - 1; i >= 0; i-- {
		kind := "TRIGGER"
		if i < 3 {
			kind = "TABLE"
		}
		if _, err := tx.Exec("DROP " + kind + " " + targetRegistrationSchemaOrder[i]); err != nil {
			return err
		}
	}
	return nil
}
