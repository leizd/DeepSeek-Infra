package store

import (
	"database/sql"
	"fmt"
	"sort"
	"strings"
)

// Schema v15 adds `control_operator_mutations`: the append-only journal for mutations an
// **authenticated operator** applies through the public control API.
//
// # Why a separate table instead of a new `result_status`
//
// `control_operations.result_status` is constrained to `('PROPOSED','APPLIED')`, and
// `APPLIED` means "a deployment-signed `control-mutation-request-v2` was verified and
// applied". An operator mutation is not that: there is no external signature, its
// authorization is the operator's authenticated session, and the ADR keeps the signed
// channel as the audited one. Widening the CHECK would make the two indistinguishable in
// the journal; a separate table keeps each row's provenance unambiguous, and a reviewer
// can always answer "was this signed, or was it an operator?" from the schema alone.
//
// The table is append-only in the same way the other journals are: `UPDATE` and `DELETE`
// are refused by trigger, so an applied operator mutation cannot be rewritten after the
// fact. It records the `actionId` and the `executionEpoch` the write was admitted under,
// the cutover revision and fencing token it was checked against, and the operator
// subject — the full chain a reconciliation needs to tie the record to its authority.
var operatorMutationSchemaObjects = func() map[string]string {
	const table = "control_operator_mutations"
	objects := map[string]string{
		table: `CREATE TABLE control_operator_mutations (
			operation_id TEXT PRIMARY KEY CHECK(length(operation_id)>0),
			domain TEXT NOT NULL CHECK(length(domain)>0),
			record_id TEXT NOT NULL CHECK(length(record_id)>0),
			record_revision INTEGER NOT NULL CHECK(record_revision>=1),
			record_state TEXT NOT NULL CHECK(length(record_state)>0),
			payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
			request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
			action_id TEXT NOT NULL CHECK(length(action_id)>0),
			execution_epoch INTEGER NOT NULL CHECK(execution_epoch>=0),
			cutover_revision INTEGER NOT NULL CHECK(cutover_revision>=0),
			fencing_token INTEGER NOT NULL CHECK(fencing_token>=1),
			writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>=1),
			actor TEXT NOT NULL CHECK(length(actor)>0),
			recorded_at INTEGER NOT NULL CHECK(recorded_at>=0)
		) STRICT`,
	}
	for _, operation := range []string{"UPDATE", "DELETE"} {
		name := table + "_no_" + strings.ToLower(operation)
		objects[name] = "CREATE TRIGGER " + name + " BEFORE " + operation + " ON " + table +
			" BEGIN SELECT RAISE(ABORT,'OPERATOR_MUTATION_IMMUTABLE'); END"
	}
	return objects
}()

// operatorMutationTable is the journal's object name, kept next to the objects so the
// verifier, the rollback path and the tests cannot disagree about it.
const operatorMutationTable = "control_operator_mutations"

func (store *Control) migrateToV15Tx(tx *sql.Tx) error {
	statements := []string{
		`CREATE TABLE control_store_meta_v15 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 15),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v15 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v15 RENAME TO control_store_meta`,
		operatorMutationSchemaObjects[operatorMutationTable],
	}
	names := make([]string, 0, len(operatorMutationSchemaObjects))
	for name := range operatorMutationSchemaObjects {
		if name != operatorMutationTable {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		statements = append(statements, operatorMutationSchemaObjects[name])
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
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(15,?,?)", now,
		"add the append-only operator mutation journal beside the signed operation journal"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=15 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=15"); err != nil {
		return err
	}
	store.schema = SchemaV15
	return nil
}

func verifyOperatorMutationSchemaTx(tx *sql.Tx) error {
	for name, expected := range operatorMutationSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil ||
			strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: operator mutation schema %s", ErrForeignRuntimeStore, name)
		}
	}
	return nil
}

// validateOperatorMutationHistoryTx refuses a rollback that would discard applied
// operator mutations. Schema-0 rollback is a "start over" operation, and dropping the
// journal silently would erase the only record that a human changed control state.
func validateOperatorMutationHistoryTx(tx *sql.Tx) error {
	var rows int
	if err := tx.QueryRow("SELECT COUNT(*) FROM " + operatorMutationTable).Scan(&rows); err != nil {
		return err
	}
	if rows != 0 {
		return ErrOperatorMutationHistoryRetained
	}
	return nil
}
