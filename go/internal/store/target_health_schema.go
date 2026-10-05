package store

import (
	"database/sql"
	"fmt"
	"sort"
	"strings"
)

// Schema v14 holds the scheduler health snapshot in the Go-owned database.
// Migrating an older store creates empty tables, not invented source evidence.
var targetHealthSchemaObjects = func() map[string]string {
	objects := map[string]string{
		"backup_target_health": `CREATE TABLE backup_target_health (
			target_id TEXT PRIMARY KEY CHECK(length(target_id)>0),
			status TEXT NOT NULL CHECK(length(status)>0),
			checked_at TEXT NOT NULL CHECK(length(checked_at)>0), detail TEXT
		) STRICT`,
		"control_target_health_imports": `CREATE TABLE control_target_health_imports (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1),
			manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
			transfer_id TEXT NOT NULL CHECK(length(transfer_id)>0),
			health_digest TEXT NOT NULL CHECK(length(health_digest)=64),
			row_count INTEGER NOT NULL CHECK(row_count>=0),
			scheduler_source_path TEXT NOT NULL CHECK(length(scheduler_source_path)>0),
			writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>=1),
			recorded_at INTEGER NOT NULL CHECK(recorded_at>=0)
		) STRICT`,
	}
	for _, table := range []string{"backup_target_health", "control_target_health_imports"} {
		for _, operation := range []string{"UPDATE", "DELETE"} {
			name := table + "_no_" + strings.ToLower(operation)
			objects[name] = "CREATE TRIGGER " + name + " BEFORE " + operation + " ON " + table +
				" BEGIN SELECT RAISE(ABORT,'TARGET_HEALTH_IMPORT_IMMUTABLE'); END"
		}
		key := "target_id=NEW.target_id"
		if table == "control_target_health_imports" {
			key = "singleton=NEW.singleton"
		}
		name := table + "_no_replace"
		objects[name] = "CREATE TRIGGER " + name + " BEFORE INSERT ON " + table +
			" WHEN EXISTS(SELECT 1 FROM " + table + " WHERE " + key + ")" +
			" BEGIN SELECT RAISE(ABORT,'TARGET_HEALTH_IMPORT_IMMUTABLE'); END"
	}
	return objects
}()

func (store *Control) migrateToV14Tx(tx *sql.Tx) error {
	statements := []string{
		`CREATE TABLE control_store_meta_v14 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 14),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v14 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v14 RENAME TO control_store_meta`,
		targetHealthSchemaObjects["backup_target_health"], targetHealthSchemaObjects["control_target_health_imports"],
	}
	names := make([]string, 0)
	for name := range targetHealthSchemaObjects {
		if name != "backup_target_health" && name != "control_target_health_imports" {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		statements = append(statements, targetHealthSchemaObjects[name])
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
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(14,?,?)", now, "bind fenced scheduler target health to target inventory"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=14 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=14"); err != nil {
		return err
	}
	store.schema = SchemaV14
	return nil
}

func verifyTargetHealthSchemaTx(tx *sql.Tx) error {
	for name, expected := range targetHealthSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil ||
			strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: target health schema %s", ErrForeignRuntimeStore, name)
		}
	}
	_, err := verifiedTargetHealthTx(tx)
	return err
}
