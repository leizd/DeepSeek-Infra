package store

import (
	"database/sql"
	"fmt"
	"strings"
)

// Schema v12 records an attested handback of one transferred inventory domain.
//
// A handback is the reverse of the offline import: Go removes the imported
// records, their event journal and the import provenance row in one
// transaction, and records the transfer identity and digests in an append-only
// handback journal. The Python source can then verify that exact document and
// lift its own write fence, so ownership returns to Python without either side
// holding a stale authoritative copy.
var inventoryHandbackSchemaObjects = map[string]string{
	"control_inventory_handbacks": `CREATE TABLE control_inventory_handbacks (
		handback_id INTEGER PRIMARY KEY AUTOINCREMENT,
		domain TEXT NOT NULL CHECK(domain IN ('policy','target')),
		transfer_id TEXT NOT NULL CHECK(length(transfer_id)>0),
		manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64 AND manifest_digest=lower(manifest_digest)),
		source_digest TEXT NOT NULL CHECK(length(source_digest)=64 AND source_digest=lower(source_digest)),
		authority_generation INTEGER NOT NULL CHECK(authority_generation>=1),
		authority_digest TEXT NOT NULL CHECK(length(authority_digest)=64 AND authority_digest=lower(authority_digest)),
		rolled_back_records INTEGER NOT NULL CHECK(rolled_back_records>=0),
		rolled_back_events INTEGER NOT NULL CHECK(rolled_back_events>=0),
		cutover_revision INTEGER NOT NULL CHECK(cutover_revision>=1),
		cutover_epoch INTEGER NOT NULL CHECK(cutover_epoch>=1),
		handback_digest TEXT NOT NULL CHECK(length(handback_digest)=64 AND handback_digest=lower(handback_digest)),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>=1),
		recorded_at INTEGER NOT NULL CHECK(recorded_at>=0),
		UNIQUE(domain, transfer_id)
	) STRICT`,
	"control_inventory_handbacks_no_update": `CREATE TRIGGER control_inventory_handbacks_no_update BEFORE UPDATE ON control_inventory_handbacks
	BEGIN SELECT RAISE(ABORT,'INVENTORY_HANDBACK_IMMUTABLE'); END`,
	"control_inventory_handbacks_no_delete": `CREATE TRIGGER control_inventory_handbacks_no_delete BEFORE DELETE ON control_inventory_handbacks
	BEGIN SELECT RAISE(ABORT,'INVENTORY_HANDBACK_IMMUTABLE'); END`,
	"control_inventory_handbacks_no_replace": `CREATE TRIGGER control_inventory_handbacks_no_replace BEFORE INSERT ON control_inventory_handbacks
	WHEN EXISTS(SELECT 1 FROM control_inventory_handbacks WHERE domain=NEW.domain AND transfer_id=NEW.transfer_id)
	BEGIN SELECT RAISE(ABORT,'INVENTORY_HANDBACK_IMMUTABLE'); END`,
}

func (store *Control) migrateToV12Tx(tx *sql.Tx) error {
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v12 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 12),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v12 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v12 RENAME TO control_store_meta`,
		inventoryHandbackSchemaObjects["control_inventory_handbacks"],
		inventoryHandbackSchemaObjects["control_inventory_handbacks_no_update"],
		inventoryHandbackSchemaObjects["control_inventory_handbacks_no_delete"],
		inventoryHandbackSchemaObjects["control_inventory_handbacks_no_replace"],
	} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(12,?,?)", now, "record attested inventory handback provenance"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=12 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=12"); err != nil {
		return err
	}
	store.schema = SchemaV12
	return nil
}

func verifyInventoryHandbackSchemaTx(tx *sql.Tx) error {
	for name, expected := range inventoryHandbackSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil ||
			strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: inventory handback schema %s", ErrForeignRuntimeStore, name)
		}
	}
	return nil
}
