package store

import (
	"database/sql"
	"strings"
)

// Version 19 remains an immutable historical schema. Version 20 changes only
// its media protobuf bounds; operation identities, states and guards stay exact.
var mediaCapacitySchemaObjects = func() map[string]string {
	objects := make(map[string]string, len(mediaPatchSchemaObjects))
	for name, statement := range mediaPatchSchemaObjects {
		objects[name] = statement
	}
	table := strings.ReplaceAll(objects["control_media_operations"], "2097152", "4194304")
	objects["control_media_operations"] = strings.Replace(table, "CREATE TABLE control_media_operations (", `CREATE TABLE "control_media_operations" (`, 1)
	return objects
}()

func (store *Control) migrateToV20Tx(tx *sql.Tx) error {
	if err := verifySchemaTx(tx, SchemaV19); err != nil {
		return err
	}
	const columns = "rowid,operation_id,intent,request_sha256,action_id,epoch,owner,claim_token,writer_fencing_token,lease_until,fleet_id,environment,state,preparation_sha256,outcome,created_at,updated_at"
	statements := []string{
		strings.Replace(mediaCapacitySchemaObjects["control_media_operations"], `"control_media_operations"`, `"control_media_operations_v20"`, 1),
		"INSERT INTO control_media_operations_v20 (" + columns + ") SELECT " + columns + " FROM control_media_operations",
		"DROP TABLE control_media_operations",
		"ALTER TABLE control_media_operations_v20 RENAME TO control_media_operations",
	}
	for _, name := range mediaPatchSchemaOrder[1:] {
		statements = append(statements, mediaCapacitySchemaObjects[name])
	}
	statements = append(statements,
		`CREATE TABLE control_store_meta_v20 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 20),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v20 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v20 RENAME TO control_store_meta`,
	)
	for _, statement := range statements {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(20,?,?)", now, "preserve original media admission history and align typed media persistence with its bounded 4 MiB transport"); err != nil {
		return err
	}
	for _, statement := range []string{"UPDATE control_store_meta SET schema_version=20 WHERE singleton=1", "PRAGMA user_version=20"} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	store.schema = SchemaV20
	return verifySchemaTx(tx, SchemaV20)
}
