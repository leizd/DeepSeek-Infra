package store

import (
	"database/sql"
	"fmt"
	"strings"
)

// Schema v9 widens the control operation journal's result status. Through v8 the
// table froze `result_status = 'PROPOSED'`, which encoded "nothing is ever
// applied". The approved control-mutation-request-v2 channel does apply, so the
// journal must be able to record that truthfully instead of reporting a proposal
// for an applied mutation.
//
// The table is otherwise identical, every row is preserved, and the frozen
// immutability triggers are recreated from the same definitions.
const controlOperationsV9Schema = `CREATE TABLE control_operations (
	operation_id TEXT PRIMARY KEY CHECK(length(operation_id) = 64 AND operation_id = lower(operation_id)),
	request_id TEXT NOT NULL UNIQUE CHECK(length(request_id) = 64 AND request_id = lower(request_id)),
	nonce TEXT NOT NULL UNIQUE CHECK(length(nonce) = 64 AND nonce = lower(nonce)),
	domain TEXT NOT NULL CHECK(domain IN (
		'policy', 'target', 'scheduler_run', 'action', 'risk', 'wave',
		'peer', 'grant', 'session', 'transfer', 'forecast', 'agent_run'
	)),
	action_id TEXT NOT NULL CHECK(length(action_id) > 0),
	execution_epoch INTEGER NOT NULL CHECK(execution_epoch > 0),
	fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
	payload_digest TEXT NOT NULL CHECK(length(payload_digest) > 0),
	request_digest TEXT NOT NULL CHECK(length(request_digest) > 0),
	canonical_request TEXT NOT NULL CHECK(json_valid(canonical_request) AND length(canonical_request) > 0),
	result_status TEXT NOT NULL CHECK(result_status IN ('PROPOSED', 'APPLIED')),
	result_json TEXT NOT NULL CHECK(json_valid(result_json)),
	writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token > 0),
	recorded_at INTEGER NOT NULL CHECK(recorded_at >= 0)
) STRICT`

const controlOperationsV9Columns = `operation_id, request_id, nonce, domain, action_id, execution_epoch,
	fencing_token, payload_digest, request_digest, canonical_request, result_status, result_json,
	writer_fencing_token, recorded_at`

func (store *Control) migrateToV9Tx(tx *sql.Tx) error {
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v9 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 9),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v9 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v9 RENAME TO control_store_meta`,
		// The immutability triggers must go before the table can be rebuilt, and
		// are recreated from the frozen definitions below.
		`DROP TRIGGER control_operations_no_update`,
		`DROP TRIGGER control_operations_no_delete`,
		`ALTER TABLE control_operations RENAME TO control_operations_v8`,
		controlOperationsV9Schema,
		`INSERT INTO control_operations(` + controlOperationsV9Columns + `)
		 SELECT ` + controlOperationsV9Columns + ` FROM control_operations_v8`,
		`DROP TABLE control_operations_v8`,
		controlOperationImmutabilityTriggers[0],
		controlOperationImmutabilityTriggers[1],
	} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(9,?,?)", now, "admit an applied control operation result"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=9 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=9"); err != nil {
		return err
	}
	store.schema = SchemaV9
	return nil
}

// verifyControlOperationStatusSchemaTx refuses a store whose operation journal
// still cannot record an applied result: reading such a store would make an
// applied mutation look like a proposal.
func verifyControlOperationStatusSchemaTx(tx *sql.Tx) error {
	var definition string
	if err := tx.QueryRow(
		"SELECT sql FROM sqlite_schema WHERE type='table' AND name='control_operations'",
	).Scan(&definition); err != nil {
		return fmt.Errorf("%w: control operation journal", ErrForeignRuntimeStore)
	}
	if !strings.Contains(definition, "'APPLIED'") {
		return fmt.Errorf("%w: control operation journal cannot record an applied result", ErrForeignRuntimeStore)
	}
	return nil
}
