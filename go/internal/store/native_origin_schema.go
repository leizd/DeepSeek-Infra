package store

import (
	"database/sql"
	"fmt"
	"strings"
)

var nativeOriginSchemaObjects = map[string]string{
	"control_native_origin": `CREATE TABLE control_native_origin (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        origin BLOB NOT NULL CHECK(length(origin)>0 AND length(origin)<=16384),
        state TEXT NOT NULL CHECK(state IN ('pending','published')),
        publication BLOB,
        CHECK((state='pending' AND publication IS NULL) OR (state='published' AND length(publication)>0 AND length(publication)<=16384))
    ) STRICT`,
	"control_native_origin_no_delete": `CREATE TRIGGER control_native_origin_no_delete BEFORE DELETE ON control_native_origin
        BEGIN SELECT RAISE(ABORT,'NATIVE_ORIGIN_IMMUTABLE'); END`,
	"control_native_origin_publication_once": `CREATE TRIGGER control_native_origin_publication_once BEFORE UPDATE ON control_native_origin
        WHEN OLD.state<>'pending' OR NEW.state<>'published' OR NEW.singleton<>OLD.singleton OR NEW.origin<>OLD.origin
        BEGIN SELECT RAISE(ABORT,'NATIVE_ORIGIN_IMMUTABLE'); END`,
}

func (store *Control) migrateToV18Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV17); err != nil {
		return err
	}
	statements := []string{
		`CREATE TABLE control_store_meta_v18 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 18),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v18 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v18 RENAME TO control_store_meta`,
		nativeOriginSchemaObjects["control_native_origin"], nativeOriginSchemaObjects["control_native_origin_no_delete"], nativeOriginSchemaObjects["control_native_origin_publication_once"],
	}
	for _, statement := range statements {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	if store.nativeNew {
		if _, err := tx.Exec("INSERT INTO control_native_origin VALUES(1,?,'pending',NULL)", nativeBytes(store.nativeOrigin.receipt)); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	for _, statement := range []string{"UPDATE control_store_meta SET schema_version=18 WHERE singleton=1", "PRAGMA user_version=18"} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(18,?,?)", now, "retain independent new-native-origin birth and publication, without manufacturing Python migration provenance"); err != nil {
		return err
	}
	store.schema = SchemaV18
	return verifyNativeOriginSchemaTx(tx)
}
func verifyNativeOriginSchemaTx(tx *sql.Tx) error {
	for name, expected := range nativeOriginSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: native origin schema %s", ErrForeignRuntimeStore, name)
		}
	}
	_, err := verifyNativeOriginTx(tx)
	return err
}
