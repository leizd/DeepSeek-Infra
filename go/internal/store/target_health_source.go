package store

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	modernsqlite "modernc.org/sqlite"
)

var targetHealthSourceFenceObjects = func() map[string]string {
	objects := map[string]string{"native_target_health_handoff_fence": `CREATE TABLE native_target_health_handoff_fence (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), transfer_id TEXT NOT NULL,
    authority_digest TEXT NOT NULL, target_source_digest TEXT NOT NULL,
    health_digest TEXT NOT NULL, created_at INTEGER NOT NULL
)`}
	for _, operation := range []string{"INSERT", "UPDATE", "DELETE"} {
		name := "native_target_health_no_" + strings.ToLower(operation)
		objects[name] = "CREATE TRIGGER " + name + " BEFORE " + operation + " ON backup_target_health " +
			"WHEN EXISTS(SELECT 1 FROM native_target_health_handoff_fence) " +
			"BEGIN SELECT RAISE(ABORT,'NATIVE_TARGET_HEALTH_HANDOFF'); END"
		name = "native_target_health_fence_no_" + strings.ToLower(operation)
		objects[name] = "CREATE TRIGGER " + name + " BEFORE " + operation + " ON native_target_health_handoff_fence " +
			"BEGIN SELECT RAISE(ABORT,'NATIVE_TARGET_HEALTH_HANDOFF_IMMUTABLE'); END"
	}
	return objects
}()

func resolveTargetHealthSource(controlPath, explicit string) (string, error) {
	if explicit == "" {
		if filepath.Base(filepath.Dir(controlPath)) != ".backup-control" {
			return "", ErrPythonSourceFenceInvalid
		}
		explicit = filepath.Join(filepath.Dir(filepath.Dir(controlPath)), ".backup-scheduler", "scheduler.db")
	}
	if !filepath.IsAbs(explicit) || filepath.Clean(explicit) == filepath.Clean(controlPath) {
		return "", ErrPythonSourceFenceInvalid
	}
	return explicit, nil
}

func attestTargetHealthSource(path string, manifest pythonInventoryExport) error {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return ErrPythonSourceFenceInvalid
	}
	query := hardenedControlQuery()
	query.Set("mode", "ro")
	query.Set("_query_only", "1")
	connector, err := modernsqlite.NewConnector(controlDatabaseURL(path, query))
	if err != nil {
		return fmt.Errorf("%w: scheduler source: %v", ErrPythonSourceFenceInvalid, err)
	}
	db := sql.OpenDB(connector)
	defer db.Close()
	db.SetMaxOpenConns(1)
	tx, err := db.BeginTx(context.Background(), &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return ErrPythonSourceFenceInvalid
	}
	defer tx.Rollback()
	if err := verifyPythonTargetHealthTableTx(tx); err != nil {
		return err
	}
	for name, expected := range targetHealthSourceFenceObjects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil ||
			strings.Join(strings.Fields(actual), " ") != strings.Join(strings.Fields(expected), " ") {
			return fmt.Errorf("%w: scheduler fence %s", ErrPythonSourceFenceInvalid, name)
		}
	}
	var transfer, authority, source, health string
	var count int
	if err := tx.QueryRow("SELECT COUNT(*) FROM native_target_health_handoff_fence").Scan(&count); err != nil || count != 1 {
		return ErrPythonSourceFenceInvalid
	}
	if err := tx.QueryRow(`SELECT transfer_id,authority_digest,target_source_digest,health_digest
		FROM native_target_health_handoff_fence WHERE singleton=1`).Scan(&transfer, &authority, &source, &health); err != nil ||
		transfer != manifest.TransferID || authority != manifest.AuthorityDigest || source != manifest.SourceDigest ||
		health != manifest.TargetHealth.SourceDigest {
		return ErrPythonSourceFenceInvalid
	}
	rows, err := readTargetHealthRowsTx(tx)
	if err != nil {
		return fmt.Errorf("%w: scheduler rows: %v", ErrPythonSourceChanged, err)
	}
	digest, err := hashCanonicalJSON(rows)
	if err != nil || digest != health {
		return ErrPythonSourceChanged
	}
	return tx.Commit()
}

func verifyPythonTargetHealthTableTx(tx *sql.Tx) error {
	rows, err := tx.Query("PRAGMA table_info(backup_target_health)")
	if err != nil {
		return ErrPythonSourceFenceInvalid
	}
	defer rows.Close()
	expected := []struct {
		name             string
		notNull, primary int
	}{{"target_id", 0, 1}, {"status", 1, 0}, {"checked_at", 1, 0}, {"detail", 0, 0}}
	index := 0
	for rows.Next() {
		var ordinal, notNull, primary int
		var name, kind string
		var defaultValue any
		if err := rows.Scan(&ordinal, &name, &kind, &notNull, &defaultValue, &primary); err != nil ||
			index >= len(expected) || ordinal != index || kind != "TEXT" || name != expected[index].name ||
			notNull != expected[index].notNull || primary != expected[index].primary {
			return ErrPythonSourceFenceInvalid
		}
		index++
	}
	if rows.Err() != nil || index != len(expected) {
		return ErrPythonSourceFenceInvalid
	}
	return nil
}

func readTargetHealthRowsTx(tx *sql.Tx) ([]map[string]any, error) {
	rows, err := tx.Query("SELECT target_id,status,checked_at,detail FROM backup_target_health ORDER BY target_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := make([]map[string]any, 0)
	previous := ""
	for rows.Next() {
		var idValue, statusValue, checkedValue, detail any
		if err := rows.Scan(&idValue, &statusValue, &checkedValue, &detail); err != nil {
			return nil, err
		}
		id, idOK := idValue.(string)
		status, statusOK := statusValue.(string)
		checked, checkedOK := checkedValue.(string)
		if !idOK || !validHealthTargetID(id) || id <= previous || !statusOK || status == "" || !checkedOK || checked == "" {
			return nil, ErrCorruptRecord
		}
		if detail != nil {
			if _, ok := detail.(string); !ok {
				return nil, ErrCorruptRecord
			}
		}
		previous = id
		result = append(result, map[string]any{"target_id": id, "status": status, "checked_at": checked, "detail": detail})
	}
	return result, rows.Err()
}
