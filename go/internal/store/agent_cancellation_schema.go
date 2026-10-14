package store

import (
	"database/sql"
	"fmt"
	"strings"
)

// v5 remains an immutable legacy definition. v16 permits only the additional
// typed cancellation terminal value; all original columns/checks are retained.
var actionCancellationLeaseSchema = strings.Replace(strings.Replace(actionAdmissionSchemaObjects["action_leases"],
	"CREATE TABLE action_leases", `CREATE TABLE "action_leases"`, 1),
	"'SUCCEEDED','FAILED_BEFORE_EFFECT'", "'SUCCEEDED','FAILED_BEFORE_EFFECT','CANCELLED_CONFIRMED'", 1)

func (store *Control) migrateToV16Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV15); err != nil {
		return err
	}
	statements := []string{
		`CREATE TABLE control_store_meta_v16 (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
 mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 16),
 unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v16 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v16 RENAME TO control_store_meta`,
		strings.Replace(actionCancellationLeaseSchema, `"action_leases"`, "action_leases_v16", 1),
		`INSERT INTO action_leases_v16 SELECT action_id,owner,epoch,claim_token,lease_until,acquired_at,updated_at,claim_revision,writer_fencing_token,terminal_state FROM action_leases`,
		`DROP TABLE action_leases`, `ALTER TABLE action_leases_v16 RENAME TO action_leases`,
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
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(16,?,?)", now, "permit independently verified cancelled Agent phase settlement without task success"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=16 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=16"); err != nil {
		return err
	}
	if err := verifyActionCancellationSchemaTx(tx); err != nil {
		return err
	}
	store.schema = SchemaV16
	return nil
}
func verifyActionCancellationSchemaTx(tx *sql.Tx) error {
	for name, expected := range actionAdmissionSchemaObjects {
		if name == "action_leases" {
			expected = actionCancellationLeaseSchema
		}
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: v16 action cancellation schema %s", ErrForeignRuntimeStore, name)
		}
	}
	return nil
}
