package store

import (
	"database/sql"
	"fmt"
	"strings"
)

// v5 and v16 remain immutable historical definitions. A proved provider failure
// is distinct from both a pre-effect rejection and an unresolved remote effect.
var actionFailureLeaseSchema = strings.Replace(actionCancellationLeaseSchema,
	"'CANCELLED_CONFIRMED'", "'CANCELLED_CONFIRMED','FAILED_CONFIRMED'", 1)

func (store *Control) migrateToV17Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV16); err != nil {
		return err
	}
	statements := []string{
		`CREATE TABLE control_store_meta_v17 (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
 mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 17),
 unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v17 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v17 RENAME TO control_store_meta`,
		strings.Replace(actionFailureLeaseSchema, `"action_leases"`, "action_leases_v17", 1),
		`INSERT INTO action_leases_v17 SELECT action_id,owner,epoch,claim_token,lease_until,acquired_at,updated_at,claim_revision,writer_fencing_token,terminal_state FROM action_leases`,
		`DROP TABLE action_leases`, `ALTER TABLE action_leases_v17 RENAME TO action_leases`,
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
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(17,?,?)", now, "permit independently verified conclusive Agent phase failure without classifying an unknown effect as absent"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=17 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=17"); err != nil {
		return err
	}
	if err := verifyActionFailureSchemaTx(tx); err != nil {
		return err
	}
	store.schema = SchemaV17
	return nil
}

func verifyActionFailureSchemaTx(tx *sql.Tx) error {
	for name, expected := range actionAdmissionSchemaObjects {
		if name == "action_leases" {
			expected = actionFailureLeaseSchema
		}
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: v17 conclusive action failure schema %s", ErrForeignRuntimeStore, name)
		}
	}
	return nil
}
