package store

import "database/sql"

// Schema v13 keeps the exact source location and manifest bytes that produced
// an attested import. Older v11/v12 imports migrate with NULL binding fields:
// they retain their data but cannot make a first ownership transfer until a
// live source has been bound again. The existing import immutability triggers
// protect the added fields as well as the original provenance.
var inventoryImportSchemaObjectsV13 = map[string]string{
	"control_inventory_imports": `CREATE TABLE control_inventory_imports (
		domain TEXT PRIMARY KEY CHECK(domain IN ('policy','target')),
		transfer_id TEXT NOT NULL CHECK(length(transfer_id)>0),
		manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64 AND manifest_digest=lower(manifest_digest)),
		source_digest TEXT NOT NULL CHECK(length(source_digest)=64 AND source_digest=lower(source_digest)),
		authority_generation INTEGER NOT NULL CHECK(authority_generation>=1),
		authority_digest TEXT NOT NULL CHECK(length(authority_digest)=64 AND authority_digest=lower(authority_digest)),
		source_schema_version INTEGER NOT NULL CHECK(source_schema_version=8),
		source_boot_epoch INTEGER NOT NULL CHECK(source_boot_epoch>=0),
		row_count INTEGER NOT NULL CHECK(row_count>=0),
		source_attested INTEGER NOT NULL CHECK(source_attested IN (0,1)),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>=1),
		recorded_at INTEGER NOT NULL CHECK(recorded_at>=0),
		source_path TEXT,
		projection_directory TEXT,
		manifest_bytes BLOB,
		CHECK(
			(source_path IS NULL AND projection_directory IS NULL AND manifest_bytes IS NULL) OR
			(source_attested=1 AND source_path IS NOT NULL AND length(source_path)>0 AND
			 projection_directory IS NOT NULL AND manifest_bytes IS NOT NULL AND length(manifest_bytes)>0)
		)
	) STRICT`,
	"control_inventory_imports_no_update": inventoryImportSchemaObjects["control_inventory_imports_no_update"],
	"control_inventory_imports_no_delete": inventoryImportSchemaObjects["control_inventory_imports_no_delete"],
}

func (store *Control) migrateToV13Tx(tx *sql.Tx) error {
	// A v12 promotion checked only stored import digests. Do not upgrade such
	// history as though the source and projection had been reattested at the
	// first ownership transfer. The old store is left intact for explicit
	// recovery instead of gaining invented v13 evidence.
	var promotedImports int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM control_inventory_imports AS import
		JOIN control_promotion_artifacts AS promotion ON promotion.domain=import.domain`).Scan(&promotedImports); err != nil {
		return err
	}
	if promotedImports != 0 {
		return ErrInventoryPromotionUnproven
	}
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v13 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 13),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v13 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v13 RENAME TO control_store_meta`,
		`DROP TRIGGER control_inventory_imports_no_update`,
		`DROP TRIGGER control_inventory_imports_no_delete`,
		`ALTER TABLE control_inventory_imports RENAME TO control_inventory_imports_v11`,
		inventoryImportSchemaObjectsV13["control_inventory_imports"],
		`INSERT INTO control_inventory_imports(
			domain,transfer_id,manifest_digest,source_digest,authority_generation,
			authority_digest,source_schema_version,source_boot_epoch,row_count,source_attested,
			writer_fencing_token,recorded_at)
		 SELECT domain,transfer_id,manifest_digest,source_digest,authority_generation,
			authority_digest,source_schema_version,source_boot_epoch,row_count,source_attested,
			writer_fencing_token,recorded_at FROM control_inventory_imports_v11`,
		`DROP TABLE control_inventory_imports_v11`,
		inventoryImportSchemaObjectsV13["control_inventory_imports_no_update"],
		inventoryImportSchemaObjectsV13["control_inventory_imports_no_delete"],
	} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(13,?,?)", now,
		"bind live Python inventory source for first ownership transfer"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=13 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=13"); err != nil {
		return err
	}
	store.schema = SchemaV13
	return nil
}
