package store

import (
	"database/sql"
	"fmt"
	"strings"
)

// Schema v8 persists the control authority itself: the monotonic
// control-authority-v1 head, its append-only checkpoint journal, and the
// append-only record of which authority tip authorized each control-domain
// promotion. The v7 verification boundary stays unchanged.
//
// The head is the only mutable row: advancing it is the single mechanism by
// which a process becomes able to authorize a production control cutover.
// Both journals are insert-only so a promotion can never be re-pointed at a
// different authority document after the fact.
var controlAuthoritySchemaObjects = map[string]string{
	"control_authority_head": `CREATE TABLE control_authority_head (
		singleton INTEGER PRIMARY KEY CHECK(singleton=1),
		authority_generation INTEGER NOT NULL CHECK(authority_generation>=1),
		digest TEXT NOT NULL CHECK(length(digest)=64),
		schema TEXT NOT NULL CHECK(length(schema)>0),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>=1),
		updated_at INTEGER NOT NULL CHECK(updated_at>=0)
	) STRICT`,
	"control_authority_head_no_delete": `CREATE TRIGGER control_authority_head_no_delete BEFORE DELETE ON control_authority_head
	BEGIN SELECT RAISE(ABORT,'AUTHORITY_HEAD_IMMUTABLE'); END`,
	"control_authority_checkpoints": `CREATE TABLE control_authority_checkpoints (
		authority_generation INTEGER PRIMARY KEY CHECK(authority_generation>=1),
		digest TEXT NOT NULL CHECK(length(digest)=64),
		previous_digest TEXT,
		payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
		document TEXT NOT NULL CHECK(length(document)>0),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>=1),
		recorded_at INTEGER NOT NULL CHECK(recorded_at>=0)
	) STRICT`,
	"control_authority_checkpoints_no_update": `CREATE TRIGGER control_authority_checkpoints_no_update BEFORE UPDATE ON control_authority_checkpoints
	BEGIN SELECT RAISE(ABORT,'AUTHORITY_CHECKPOINT_IMMUTABLE'); END`,
	"control_authority_checkpoints_no_delete": `CREATE TRIGGER control_authority_checkpoints_no_delete BEFORE DELETE ON control_authority_checkpoints
	BEGIN SELECT RAISE(ABORT,'AUTHORITY_CHECKPOINT_IMMUTABLE'); END`,
	"control_cutover_authorizations": `CREATE TABLE control_cutover_authorizations (
		domain TEXT NOT NULL,
		transfer_id TEXT NOT NULL,
		authority_generation INTEGER NOT NULL CHECK(authority_generation>=1),
		authority_digest TEXT NOT NULL CHECK(length(authority_digest)=64),
		from_state TEXT NOT NULL,
		to_state TEXT NOT NULL,
		previous_revision INTEGER NOT NULL CHECK(previous_revision>=1),
		revision INTEGER NOT NULL CHECK(revision>=2),
		previous_epoch INTEGER NOT NULL CHECK(previous_epoch>=1),
		epoch INTEGER NOT NULL CHECK(epoch>=2),
		previous_fencing_token INTEGER NOT NULL CHECK(previous_fencing_token>=1),
		fencing_token INTEGER NOT NULL CHECK(fencing_token>=2),
		writer_fencing_token INTEGER NOT NULL CHECK(writer_fencing_token>=1),
		recorded_at INTEGER NOT NULL CHECK(recorded_at>=0),
		PRIMARY KEY(domain, transfer_id)
	) STRICT`,
	"control_cutover_authorizations_no_update": `CREATE TRIGGER control_cutover_authorizations_no_update BEFORE UPDATE ON control_cutover_authorizations
	BEGIN SELECT RAISE(ABORT,'CUTOVER_AUTHORIZATION_IMMUTABLE'); END`,
	"control_cutover_authorizations_no_delete": `CREATE TRIGGER control_cutover_authorizations_no_delete BEFORE DELETE ON control_cutover_authorizations
	BEGIN SELECT RAISE(ABORT,'CUTOVER_AUTHORIZATION_IMMUTABLE'); END`,
}

func (store *Control) migrateToV8Tx(tx *sql.Tx) error {
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v8 (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 8),
			unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v8 SELECT singleton,runtime,mode,schema_version,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`,
		`ALTER TABLE control_store_meta_v8 RENAME TO control_store_meta`,
		controlAuthoritySchemaObjects["control_authority_head"],
		controlAuthoritySchemaObjects["control_authority_head_no_delete"],
		controlAuthoritySchemaObjects["control_authority_checkpoints"],
		controlAuthoritySchemaObjects["control_authority_checkpoints_no_update"],
		controlAuthoritySchemaObjects["control_authority_checkpoints_no_delete"],
		controlAuthoritySchemaObjects["control_cutover_authorizations"],
		controlAuthoritySchemaObjects["control_cutover_authorizations_no_update"],
		controlAuthoritySchemaObjects["control_cutover_authorizations_no_delete"],
	} {
		if _, err := tx.Exec(statement); err != nil {
			return err
		}
	}
	now := store.now()
	if now < 0 {
		return ErrWriterFenceHeld
	}
	if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(8,?,?)", now, "control authority head, checkpoint journal, and cutover authorizations"); err != nil {
		return err
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=8 WHERE singleton=1"); err != nil {
		return err
	}
	if _, err := tx.Exec("PRAGMA user_version=8"); err != nil {
		return err
	}
	store.schema = SchemaV8
	return nil
}

func verifyControlAuthoritySchemaTx(tx *sql.Tx) error {
	for name, expected := range controlAuthoritySchemaObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || strings.TrimSpace(actual) != strings.TrimSpace(expected) {
			return fmt.Errorf("%w: control authority schema %s", ErrForeignRuntimeStore, name)
		}
	}
	// A head row is optional: an upgraded store that has not yet claimed
	// authority has no head. When present it must be internally consistent and
	// must be the tip of the recorded checkpoint history.
	var generation, headCount int64
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_authority_head").Scan(&headCount); err != nil {
		return err
	}
	if headCount == 0 {
		return nil
	}
	// `singleton` is a primary key with CHECK(singleton=1), so a non-zero count
	// can only be one row; no further multiplicity check is possible.
	var digest, schema string
	if err := tx.QueryRow(
		"SELECT authority_generation, digest, schema FROM control_authority_head WHERE singleton=1",
	).Scan(&generation, &digest, &schema); err != nil {
		return err
	}
	if generation < 1 || !isLowerSHA256(digest) || schema != ControlAuthoritySchema {
		return fmt.Errorf("%w: control authority head", ErrForeignRuntimeStore)
	}
	var tipDigest string
	var tipGeneration int64
	if err := tx.QueryRow(
		"SELECT authority_generation, digest FROM control_authority_checkpoints ORDER BY authority_generation DESC LIMIT 1",
	).Scan(&tipGeneration, &tipDigest); err != nil {
		return fmt.Errorf("%w: control authority history", ErrForeignRuntimeStore)
	}
	if tipGeneration != generation || tipDigest != digest {
		return fmt.Errorf("%w: control authority head is not the checkpoint tip", ErrForeignRuntimeStore)
	}
	var checkpointCount int64
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_authority_checkpoints").Scan(&checkpointCount); err != nil {
		return err
	}
	if checkpointCount != generation {
		return fmt.Errorf("%w: control authority history is not contiguous", ErrForeignRuntimeStore)
	}
	return nil
}
