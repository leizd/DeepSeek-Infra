package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"strings"
)

// Schema v10 records the exact externally signed decision in the same
// transaction as a domain promotion. Existing v8/v9 authorizations are retained
// as history but do not acquire a synthetic signature during migration.
var promotionArtifactSchemaObjects = map[string]string{
	"control_promotion_artifacts": `CREATE TABLE control_promotion_artifacts (
		domain TEXT NOT NULL,
		transfer_id TEXT NOT NULL,
		signer_key_id TEXT NOT NULL CHECK(length(signer_key_id)>0),
		artifact_digest TEXT NOT NULL CHECK(length(artifact_digest)=64),
		canonical_artifact TEXT NOT NULL CHECK(json_valid(canonical_artifact) AND length(canonical_artifact)>0),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>=1),
		recorded_at INTEGER NOT NULL CHECK(recorded_at>=0),
		PRIMARY KEY(domain, transfer_id),
		FOREIGN KEY(domain, transfer_id) REFERENCES control_cutover_authorizations(domain, transfer_id)
	) STRICT`,
	"control_promotion_artifacts_no_update": `CREATE TRIGGER control_promotion_artifacts_no_update BEFORE UPDATE ON control_promotion_artifacts
	BEGIN SELECT RAISE(ABORT,'PROMOTION_ARTIFACT_IMMUTABLE'); END`,
	"control_promotion_artifacts_no_delete": `CREATE TRIGGER control_promotion_artifacts_no_delete BEFORE DELETE ON control_promotion_artifacts
	BEGIN SELECT RAISE(ABORT,'PROMOTION_ARTIFACT_IMMUTABLE'); END`,
}

func (store *Control) migrateToV10Tx(tx *sql.Tx) error {
	// v8/v9 could promote with a checkpoint alone. Never reinterpret such a
	// historical transition as signed: an operator must fence and recover that
	// isolated store before it can be upgraded for production use.
	var unsigned int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_cutover_authorizations").Scan(&unsigned); err != nil {
		return err
	}
	if unsigned != 0 {
		return ErrUnsignedPromotionHistory
	}
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v10 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 10),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v10 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v10 RENAME TO control_store_meta`,
		promotionArtifactSchemaObjects["control_promotion_artifacts"],
		promotionArtifactSchemaObjects["control_promotion_artifacts_no_update"],
		promotionArtifactSchemaObjects["control_promotion_artifacts_no_delete"],
	} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(10,?,?)", now, "record externally signed domain promotion artifacts"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=10 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=10"); err != nil {
		return err
	}
	store.schema = SchemaV10
	return nil
}

func verifyPromotionArtifactSchemaTx(tx *sql.Tx) error {
	for name, expected := range promotionArtifactSchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: promotion artifact schema %s", ErrForeignRuntimeStore, name)
		}
	}
	return nil
}

// A current authoritative cutover must still have the authorization and exact
// signed artifact that were committed with its transition. Schema checks alone
// cannot detect rows removed after temporarily disabling immutability triggers.
func verifyCurrentPromotionBindingsTx(tx *sql.Tx) error {
	var missing int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM control_cutover AS cutover
		LEFT JOIN control_cutover_authorizations AS authorization
		  ON authorization.domain=cutover.domain AND authorization.transfer_id=cutover.transfer_id
		LEFT JOIN control_promotion_artifacts AS artifact
		  ON artifact.domain=cutover.domain AND artifact.transfer_id=cutover.transfer_id
		WHERE cutover.state IN ('go_authoritative','python_shadow','python_disabled')
		  AND (authorization.domain IS NULL OR artifact.domain IS NULL
		    OR authorization.to_state<>cutover.state OR authorization.revision<>cutover.revision
		    OR authorization.epoch<>cutover.epoch OR authorization.fencing_token<>cutover.fencing_token)`).Scan(&missing); err != nil {
		return err
	}
	if missing != 0 {
		return fmt.Errorf("%w: authoritative cutover has no matching signed promotion", ErrForeignRuntimeStore)
	}
	rows, err := tx.Query(`SELECT artifact.artifact_digest, artifact.canonical_artifact
		FROM control_cutover AS cutover
		JOIN control_promotion_artifacts AS artifact
		  ON artifact.domain=cutover.domain AND artifact.transfer_id=cutover.transfer_id
		WHERE cutover.state IN ('go_authoritative','python_shadow','python_disabled')`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var digest, canonical string
		if err := rows.Scan(&digest, &canonical); err != nil {
			return err
		}
		computed := sha256.Sum256([]byte(canonical))
		if digest != hex.EncodeToString(computed[:]) {
			return fmt.Errorf("%w: authoritative promotion artifact digest mismatch", ErrForeignRuntimeStore)
		}
	}
	return rows.Err()
}
