package store

import (
	"database/sql"
	"fmt"
	"math"
	"strings"
)

// The immutable boundary preserves the interpretation of every pre-upgrade
// event. Adding a state must not retroactively legalize a corrupt old history.
var actionReconciliationSchemaObjects = map[string]string{
	"action_reconciliation_boundary": `CREATE TABLE action_reconciliation_boundary (
		singleton INTEGER PRIMARY KEY CHECK(singleton=1),
		last_legacy_event_id INTEGER NOT NULL CHECK(last_legacy_event_id>=0)
	) STRICT`,
	"action_reconciliation_boundary_no_update": `CREATE TRIGGER action_reconciliation_boundary_no_update BEFORE UPDATE ON action_reconciliation_boundary
	BEGIN SELECT RAISE(ABORT,'RECONCILIATION_BOUNDARY_IMMUTABLE'); END`,
	"action_reconciliation_boundary_no_delete": `CREATE TRIGGER action_reconciliation_boundary_no_delete BEFORE DELETE ON action_reconciliation_boundary
	BEGIN SELECT RAISE(ABORT,'RECONCILIATION_BOUNDARY_IMMUTABLE'); END`,
	"action_reconciliation_boundary_no_replace": `CREATE TRIGGER action_reconciliation_boundary_no_replace BEFORE INSERT ON action_reconciliation_boundary
	WHEN EXISTS(SELECT 1 FROM action_reconciliation_boundary)
	BEGIN SELECT RAISE(ABORT,'RECONCILIATION_BOUNDARY_IMMUTABLE'); END`,
}

func (store *Control) migrateToV6Tx(tx *sql.Tx) error {
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v6 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 6),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v6 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v6 RENAME TO control_store_meta`,
		actionReconciliationSchemaObjects["action_reconciliation_boundary"],
		`INSERT INTO action_reconciliation_boundary SELECT 1,COALESCE(MAX(event_id),0) FROM control_events`,
		actionReconciliationSchemaObjects["action_reconciliation_boundary_no_update"],
		actionReconciliationSchemaObjects["action_reconciliation_boundary_no_delete"],
		actionReconciliationSchemaObjects["action_reconciliation_boundary_no_replace"],
	} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(6,?,?)", now, "versioned native action reconciliation"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=6 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=6"); err != nil {
		return err
	}
	store.schema = SchemaV6
	return nil
}

func verifyActionReconciliationSchemaTx(tx *sql.Tx) error {
	for name, expected := range actionReconciliationSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: action reconciliation schema %s", ErrForeignRuntimeStore, name)
		}
	}
	var boundary, maximum int64
	err := tx.QueryRow(`SELECT last_legacy_event_id,(SELECT COALESCE(MAX(event_id),0) FROM control_events)
		FROM action_reconciliation_boundary WHERE singleton=1`).Scan(&boundary, &maximum)
	if err != nil || boundary < 0 || boundary > maximum {
		return ErrForeignRuntimeStore
	}
	return nil
}

func reconciliationHistoryBoundaryTx(tx *sql.Tx) (int64, error) {
	var schema int
	if err := tx.QueryRow("SELECT schema_version FROM control_store_meta WHERE singleton=1").Scan(&schema); err != nil {
		return 0, err
	}
	if schema < SchemaV6 {
		return math.MaxInt64, nil
	}
	var boundary int64
	err := tx.QueryRow("SELECT last_legacy_event_id FROM action_reconciliation_boundary WHERE singleton=1").Scan(&boundary)
	return boundary, err
}

// Only typed native lease operations may write these edges. Generic shadow
// records and pre-v6 histories retain LegalTransition's original small graph.
func reconciliationTransition(from, to string, previousEpoch, epoch uint64) bool {
	if to == "RECONCILING" {
		return activeLeasedActionState(from) && previousEpoch < math.MaxInt64 && epoch == previousEpoch+1
	}
	return from == "RECONCILING" && epoch == previousEpoch &&
		(to == "EFFECT_UNKNOWN" || to == "SUCCEEDED" || to == "FAILED_BEFORE_EFFECT")
}

func activeLeasedActionState(state string) bool {
	return state == "CLAIMED" || state == "EXECUTING" || state == "EFFECT_UNKNOWN" || state == "RECONCILING"
}

func validateReconciliationEventTx(tx *sql.Tx, event Record, metadata storedRecordMetadata) error {
	var count int
	query := `SELECT COUNT(*) FROM action_lease_events WHERE action_id=? AND epoch=?
		AND writer_fencing_token=? AND event_type IN ('ADMITTED','TAKEOVER') AND claim_revision<?`
	args := []any{event.ID, int64(event.ExecutionEpoch), metadata.writerToken, event.Revision}
	if event.State == "RECONCILING" {
		query = `SELECT COUNT(*) FROM action_lease_events WHERE action_id=? AND epoch=?
			AND writer_fencing_token=? AND event_type='TAKEOVER' AND claim_revision=? AND recorded_at=?`
		args = append(args, metadata.timestamp)
	}
	if err := tx.QueryRow(query, args...).Scan(&count); err != nil {
		return err
	}
	if count != 1 {
		return ErrCorruptRecord
	}
	return nil
}
