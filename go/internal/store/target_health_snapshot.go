package store

import (
	"database/sql"
	"errors"
	"path/filepath"
)

type targetHealthSnapshot struct {
	ManifestDigest string
	SourcePath     string
	Health         []TargetHealth
}

func (store *Control) importTargetHealthTx(tx *sql.Tx, manifest pythonInventoryExport, source InventorySourceAttestation, now int64) error {
	if manifest.TargetHealth == nil {
		return nil
	}
	var existing int
	if err := tx.QueryRow("SELECT (SELECT COUNT(*) FROM backup_target_health)+(SELECT COUNT(*) FROM control_target_health_imports)").Scan(&existing); err != nil {
		return err
	}
	if existing != 0 {
		return ErrInventoryImportConflict
	}
	for _, row := range manifest.TargetHealth.Rows {
		if _, err := tx.Exec("INSERT INTO backup_target_health(target_id,status,checked_at,detail) VALUES(?,?,?,?)",
			row["target_id"], row["status"], row["checked_at"], row["detail"]); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(`INSERT INTO control_target_health_imports(
		singleton,manifest_digest,transfer_id,health_digest,row_count,scheduler_source_path,writer_fencing_token,recorded_at)
		VALUES(1,?,?,?,?,?,?,?)`, manifest.ManifestDigest, manifest.TransferID, manifest.TargetHealth.SourceDigest,
		len(manifest.TargetHealth.Rows), source.schedulerPath, store.token, now); err != nil {
		return err
	}
	_, err := verifiedTargetHealthTx(tx)
	return err
}

// An empty health table is usable only when a real empty source was imported.
// The import is bound to the same manifest, writer and transaction time as the
// target inventory. Recompute its exact rows on reads, including after restart.
func verifiedTargetHealthTx(tx *sql.Tx) (*targetHealthSnapshot, error) {
	var digest, transfer, healthDigest, sourcePath string
	var count int
	var token, recorded int64
	err := tx.QueryRow(`SELECT manifest_digest,transfer_id,health_digest,row_count,
		scheduler_source_path,writer_fencing_token,recorded_at FROM control_target_health_imports WHERE singleton=1`).Scan(
		&digest, &transfer, &healthDigest, &count, &sourcePath, &token, &recorded)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	missing := errors.Is(err, sql.ErrNoRows)
	var manifestBytes []byte
	var importDigest, importTransfer string
	var importToken, importTime int64
	var attested int
	err = tx.QueryRow(`SELECT manifest_bytes,manifest_digest,transfer_id,writer_fencing_token,recorded_at,source_attested
		FROM control_inventory_imports WHERE domain='target'`).Scan(&manifestBytes, &importDigest, &importTransfer, &importToken, &importTime, &attested)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	var manifest pythonInventoryExport
	if len(manifestBytes) != 0 {
		manifest, _, err = parsePythonInventoryExport(manifestBytes)
		if err != nil {
			return nil, ErrCorruptRecord
		}
	}
	if missing {
		var rows int
		if err := tx.QueryRow("SELECT COUNT(*) FROM backup_target_health").Scan(&rows); err != nil {
			return nil, err
		}
		if rows != 0 || manifest.TargetHealth != nil {
			return nil, ErrCorruptRecord
		}
		return nil, nil
	}
	if manifest.TargetHealth == nil || attested != 1 || digest != importDigest || transfer != importTransfer ||
		token != importToken || recorded != importTime || !filepath.IsAbs(sourcePath) ||
		healthDigest != manifest.TargetHealth.SourceDigest || count != len(manifest.TargetHealth.Rows) {
		return nil, ErrCorruptRecord
	}
	rows, err := readTargetHealthRowsTx(tx)
	if err != nil {
		return nil, err
	}
	computed, err := hashCanonicalJSON(rows)
	if err != nil || computed != healthDigest || len(rows) != count {
		return nil, ErrCorruptRecord
	}
	snapshot := &targetHealthSnapshot{ManifestDigest: digest, SourcePath: sourcePath, Health: make([]TargetHealth, 0, len(rows))}
	for _, row := range rows {
		health := TargetHealth{TargetID: row["target_id"].(string), Status: row["status"].(string), CheckedAt: row["checked_at"].(string)}
		if detail, ok := row["detail"].(string); ok {
			health.Detail = &detail
		}
		snapshot.Health = append(snapshot.Health, health)
	}
	return snapshot, nil
}

func rollbackTargetHealthTx(tx *sql.Tx, domain, manifestDigest string) error {
	if domain != "target" {
		return nil
	}
	snapshot, err := verifiedTargetHealthTx(tx)
	if err != nil || snapshot == nil {
		return err
	}
	if snapshot.ManifestDigest != manifestDigest {
		return ErrInventoryHandbackInvalid
	}
	names := []string{"backup_target_health_no_delete", "control_target_health_imports_no_delete"}
	statements := []string{targetHealthSchemaObjects[names[0]], targetHealthSchemaObjects[names[1]]}
	return withLiftedTriggers(tx, names, statements, func() error {
		if _, err := tx.Exec("DELETE FROM backup_target_health"); err != nil {
			return err
		}
		_, err := tx.Exec("DELETE FROM control_target_health_imports")
		return err
	})
}
