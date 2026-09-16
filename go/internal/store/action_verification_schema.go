package store

import (
	"database/sql"
	"fmt"
	"math"
	"strings"
)

// A separate immutable boundary prevents the new verification edges from
// legalizing pre-v7 histories. The v6 reconciliation boundary stays unchanged.
var actionVerificationSchemaObjects = map[string]string{
	"action_verification_boundary": `CREATE TABLE action_verification_boundary (
		singleton INTEGER PRIMARY KEY CHECK(singleton=1),
		last_legacy_event_id INTEGER NOT NULL CHECK(last_legacy_event_id>=0)
	) STRICT`,
	"action_verification_boundary_no_update": `CREATE TRIGGER action_verification_boundary_no_update BEFORE UPDATE ON action_verification_boundary
	BEGIN SELECT RAISE(ABORT,'VERIFICATION_BOUNDARY_IMMUTABLE'); END`,
	"action_verification_boundary_no_delete": `CREATE TRIGGER action_verification_boundary_no_delete BEFORE DELETE ON action_verification_boundary
	BEGIN SELECT RAISE(ABORT,'VERIFICATION_BOUNDARY_IMMUTABLE'); END`,
	"action_verification_boundary_no_replace": `CREATE TRIGGER action_verification_boundary_no_replace BEFORE INSERT ON action_verification_boundary
	WHEN EXISTS(SELECT 1 FROM action_verification_boundary)
	BEGIN SELECT RAISE(ABORT,'VERIFICATION_BOUNDARY_IMMUTABLE'); END`,
}

func (store *Control) migrateToV7Tx(tx *sql.Tx) error {
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v7 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 7),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v7 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v7 RENAME TO control_store_meta`,
		actionVerificationSchemaObjects["action_verification_boundary"],
		`INSERT INTO action_verification_boundary SELECT 1,COALESCE(MAX(event_id),0) FROM control_events`,
		actionVerificationSchemaObjects["action_verification_boundary_no_update"],
		actionVerificationSchemaObjects["action_verification_boundary_no_delete"],
		actionVerificationSchemaObjects["action_verification_boundary_no_replace"],
	} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(7,?,?)", now, "versioned native action verification phases"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=7 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=7"); err != nil {
		return err
	}
	store.schema = SchemaV7
	return nil
}

func verifyActionVerificationSchemaTx(tx *sql.Tx) error {
	for name, expected := range actionVerificationSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: action verification schema %s", ErrForeignRuntimeStore, name)
		}
	}
	var boundary, previous, maximum int64
	err := tx.QueryRow(`SELECT last_legacy_event_id,
		(SELECT last_legacy_event_id FROM action_reconciliation_boundary WHERE singleton=1),
		(SELECT COALESCE(MAX(event_id),0) FROM control_events)
		FROM action_verification_boundary WHERE singleton=1`).Scan(&boundary, &previous, &maximum)
	if err != nil || boundary < previous || boundary > maximum {
		return ErrForeignRuntimeStore
	}
	return nil
}

func verificationActionState(state string) bool {
	return state == "VERIFYING" || state == "ASSESSING_EFFECT"
}

// These are journal primitives, not proof validation. Outcome verification and
// scoped risk assessment must supply qualified evidence before production use.
func verificationTransition(from, to string, previousEpoch, epoch uint64) bool {
	if to == "RECONCILING" {
		return verificationActionState(from) && previousEpoch < math.MaxInt64 && epoch == previousEpoch+1
	}
	if epoch != previousEpoch {
		return false
	}
	switch to {
	case "VERIFYING":
		return from == "EXECUTING" || from == "RECONCILING" || from == "EFFECT_UNKNOWN"
	case "ASSESSING_EFFECT":
		return from == "VERIFYING"
	case "SUCCEEDED":
		return from == "ASSESSING_EFFECT"
	case "EFFECT_UNKNOWN":
		return verificationActionState(from)
	default:
		return false
	}
}
