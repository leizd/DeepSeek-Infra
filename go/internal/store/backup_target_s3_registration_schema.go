package store

import (
	"database/sql"
	"fmt"
)

const s3TargetRegistrationTable = "control_target_registration_s3"

var s3TargetRegistrationSchema = []struct{ name, kind, statement string }{
	{s3TargetRegistrationTable, "table", `CREATE TABLE control_target_registration_s3 (
		operation_id TEXT NOT NULL PRIMARY KEY REFERENCES control_target_registrations(operation_id),
		context_json TEXT NOT NULL CHECK(length(context_json)>0 AND length(context_json)<=8192)
	) STRICT`},
	{"control_target_registration_s3_no_update", "trigger", "CREATE TRIGGER control_target_registration_s3_no_update BEFORE UPDATE ON control_target_registration_s3 BEGIN SELECT RAISE(ABORT,'TARGET_REGISTRATION_HISTORY_IMMUTABLE'); END"},
	{"control_target_registration_s3_no_delete", "trigger", "CREATE TRIGGER control_target_registration_s3_no_delete BEFORE DELETE ON control_target_registration_s3 BEGIN SELECT RAISE(ABORT,'TARGET_REGISTRATION_HISTORY_IMMUTABLE'); END"},
	{"control_target_registration_s3_no_replace", "trigger", "CREATE TRIGGER control_target_registration_s3_no_replace BEFORE INSERT ON control_target_registration_s3 WHEN EXISTS(SELECT 1 FROM control_target_registration_s3 WHERE operation_id=NEW.operation_id OR rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'TARGET_REGISTRATION_HISTORY_IMMUTABLE'); END"},
}

func (store *Control) migrateToV24Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV23); err != nil {
		return err
	}
	statements := []string{
		"CREATE TABLE control_store_meta_v24 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 24),unique_writer TEXT NOT NULL) STRICT",
		"INSERT INTO control_store_meta_v24 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_store_meta_v24 RENAME TO control_store_meta",
	}
	for _, object := range s3TargetRegistrationSchema {
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
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(24,?,?)", now, "retain signed S3 target registration observations"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=24 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=24"); err != nil {
		return err
	}
	store.schema = SchemaV24
	return verifySchemaTx(tx, SchemaV24)
}

func verifyS3TargetRegistrationSchemaTx(tx targetMarkerSchemaEditor) error {
	for _, object := range s3TargetRegistrationSchema {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=? AND type=?", object.name, object.kind).Scan(&actual); err != nil || actual != object.statement {
			return fmt.Errorf("%w: S3 target registration object %s", ErrForeignRuntimeStore, object.name)
		}
	}
	return nil
}

func retireEmptyS3TargetRegistrationsTx(tx targetMarkerSchemaEditor) error {
	if err := verifyS3TargetRegistrationSchemaTx(tx); err != nil {
		return err
	}
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM " + s3TargetRegistrationTable).Scan(&count); err != nil {
		return err
	}
	if count != 0 {
		return ErrSchemaInactive
	}
	for index := len(s3TargetRegistrationSchema) - 1; index >= 0; index-- {
		object := s3TargetRegistrationSchema[index]
		if _, err := tx.Exec("DROP " + object.kind + " " + object.name); err != nil {
			return err
		}
	}
	return nil
}
