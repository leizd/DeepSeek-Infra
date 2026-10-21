package store

import (
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"

	"github.com/leizd/DeepSeek-Infra/go/internal/sqlitedb"
)

// NextWriterFencingToken is a read-only launch preflight, never a writer claim.
// It includes the retained WAL. The actual OpenControl claim is authoritative;
// the launcher must require that claim to equal this prediction before serving.
func NextWriterFencingToken(path string) (int64, error) {
	if err := RejectPythonPath(path); err != nil {
		return 0, err
	}
	path, err := filepath.Abs(path)
	if err != nil {
		return 0, err
	}
	database := filepath.Join(path, ControlDatabaseFilename)
	info, err := os.Lstat(database)
	if errors.Is(err, os.ErrNotExist) {
		return 1, nil
	}
	if err != nil {
		return 0, err
	}
	if !info.Mode().IsRegular() {
		return 0, ErrForeignRuntimeStore
	}
	if info.Size() == 0 {
		return nativePendingWriterPreview(path)
	}
	query := hardenedControlQuery()
	query.Set("mode", "ro")
	query.Set("_query_only", "1")
	db, err := sqlitedb.Open(controlDatabaseURL(database, query))
	if err != nil {
		return 0, err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	var runtimeName, mode, writer string
	var schema, userVersion, migrations int
	if err = db.QueryRow("SELECT runtime,mode,schema_version,unique_writer FROM control_store_meta WHERE singleton=1").Scan(&runtimeName, &mode, &schema, &writer); err != nil {
		if token, previewErr := nativePendingWriterPreview(path); previewErr == nil {
			initialized, markerErr := validateExistingControlMarker(database)
			if markerErr == nil && !initialized {
				return token, nil
			}
		}
		return 0, fmt.Errorf("%w: writer preview metadata", ErrForeignRuntimeStore)
	}
	if runtimeName != RuntimeGo || mode != ModeShadow || writer != RuntimeGo || schema < 1 || schema > CurrentSchema {
		return 0, ErrForeignRuntimeStore
	}
	if err = db.QueryRow("PRAGMA user_version").Scan(&userVersion); err != nil {
		return 0, err
	}
	if err = db.QueryRow("SELECT COUNT(*) FROM schema_migrations").Scan(&migrations); err != nil {
		return 0, err
	}
	if schema != userVersion || schema != migrations {
		return 0, ErrForeignRuntimeStore
	}
	if err = validateControlUserObjects(db, schema); err != nil {
		return 0, err
	}
	var owner string
	var token, leaseUntil int64
	if err = db.QueryRow("SELECT runtime,mode,owner_instance_id,fencing_token,lease_until FROM control_writer WHERE singleton=1").Scan(&runtimeName, &mode, &owner, &token, &leaseUntil); err != nil {
		return 0, ErrForeignRuntimeStore
	}
	if runtimeName != RuntimeGo || mode != ModeShadow || owner == "" || token <= 0 || token == math.MaxInt64 || leaseUntil < 0 {
		return 0, ErrForeignRuntimeStore
	}
	return token + 1, nil
}
