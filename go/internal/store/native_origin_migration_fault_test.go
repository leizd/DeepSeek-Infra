package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
)

func nativeV18VersionRows(t *testing.T, db *sql.DB) string {
	t.Helper()
	all := map[string][][]any{}
	for _, table := range []string{"control_store_meta", "schema_migrations"} {
		rows, err := db.Query("SELECT * FROM " + table + " ORDER BY rowid")
		if err != nil {
			t.Fatal(err)
		}
		columns, err := rows.Columns()
		if err != nil {
			t.Fatal(err)
		}
		for rows.Next() {
			values, pointers := make([]any, len(columns)), make([]any, len(columns))
			for index := range values {
				pointers[index] = &values[index]
			}
			if err := rows.Scan(pointers...); err != nil {
				t.Fatal(err)
			}
			all[table] = append(all[table], values)
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		_ = rows.Close()
	}
	encoded, err := json.Marshal(all)
	if err != nil {
		t.Fatal(err)
	}
	return string(encoded)
}

func TestNativeOriginV18MigrationFaultRetainsOriginalSignedStore(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		t.Run(phase, func(t *testing.T) {
			for _, fault := range []string{"metadata-rename", "negative-clock", "late-journal"} {
				t.Run(fault, func(t *testing.T) {
					control, input, _ := failureSettlementFixture(t, phase)
					materializeFailureV16Fixture(t, control)
					tx, err := control.db.Begin()
					if err != nil {
						t.Fatal(err)
					}
					if err = control.migrateToV17Tx(tx); err != nil {
						_ = tx.Rollback()
						t.Fatal(err)
					}
					if err = tx.Commit(); err != nil {
						t.Fatal(err)
					}
					business, versions := failureMigrationRows(t, control.db), nativeV18VersionRows(t, control.db)
					clock := control.now
					switch fault {
					case "metadata-rename":
						_, err = control.db.Exec("CREATE TEMP VIEW native_v18_rewrite_fault AS SELECT * FROM temp.native_v18_missing_dependency")
					case "negative-clock":
						control.now = func() int64 { return -1 }
					case "late-journal":
						_, err = control.db.Exec("CREATE TEMP TRIGGER native_v18_journal_fault BEFORE INSERT ON schema_migrations WHEN NEW.version=18 BEGIN SELECT RAISE(ABORT,'native-v18-journal-fault'); END")
					}
					if err != nil {
						t.Fatal(err)
					}
					tx, err = control.db.Begin()
					if err != nil {
						t.Fatal(err)
					}
					err = control.migrateToV18Tx(tx)
					_ = tx.Rollback()
					control.now = clock
					if err == nil || fault == "negative-clock" && !errors.Is(err, ErrWriterFenceHeld) ||
						fault == "metadata-rename" && !strings.Contains(err.Error(), "native_v18_rewrite_fault") ||
						fault == "late-journal" && !strings.Contains(err.Error(), "native-v18-journal-fault") {
						t.Fatal("v18 upgrade did not hit the required interruption boundary", fault, err)
					}
					if control.SchemaVersion() != SchemaV17 || failureMigrationRows(t, control.db) != business || nativeV18VersionRows(t, control.db) != versions {
						t.Fatal("failed v18 upgrade changed protected rows, writer, version ledger or schema")
					}
					var version, nativeObjects int
					if err = control.db.QueryRow("PRAGMA user_version").Scan(&version); err != nil || version != 17 {
						t.Fatal("failed v18 upgrade advanced its durable header", err)
					}
					if err = control.db.QueryRow("SELECT COUNT(*) FROM main.sqlite_schema WHERE name LIKE 'control_native_origin%'").Scan(&nativeObjects); err != nil || nativeObjects != 0 {
						t.Fatal("failed v18 upgrade retained partial native provenance objects", err)
					}
					if fault == "metadata-rename" {
						_, err = control.db.Exec("DROP VIEW temp.native_v18_rewrite_fault")
					} else if fault == "late-journal" {
						_, err = control.db.Exec("DROP TRIGGER temp.native_v18_journal_fault")
					}
					if err != nil {
						t.Fatal(err)
					}
					tx, err = control.db.Begin()
					if err != nil {
						t.Fatal(err)
					}
					if err = verifySchemaTx(tx, SchemaV17); err == nil {
						err = control.migrateToV18Tx(tx)
					}
					if err != nil {
						_ = tx.Rollback()
						t.Fatal("original signed store could not retry the upgrade", err)
					}
					if err = tx.Commit(); err != nil {
						t.Fatal(err)
					}
					var before, after map[string]json.RawMessage
					if err = json.Unmarshal([]byte(business), &before); err != nil {
						t.Fatal(err)
					}
					if err = json.Unmarshal([]byte(failureMigrationRows(t, control.db)), &after); err != nil {
						t.Fatal(err)
					}
					origin, exists := after["control_native_origin"]
					delete(after, "control_native_origin")
					if control.SchemaVersion() != SchemaV18 || !exists || string(origin) != "null" || !reflect.DeepEqual(before, after) {
						t.Fatal("retry changed existing custody or manufactured native birth")
					}
					// The v18-specific rollback/provenance checks above stay exact.
					// Public live APIs require the current schema, so finish the
					// additive chain before reading the original retained lease.
					tx, err = control.db.Begin()
					if err != nil {
						t.Fatal(err)
					}
					if err = control.migrateTx(tx); err != nil {
						_ = tx.Rollback()
						t.Fatal(err)
					}
					if err = tx.Commit(); err != nil {
						t.Fatal(err)
					}
					if control.SchemaVersion() != CurrentSchema {
						t.Fatal("current additive schema was not reached")
					}
					lease, active, err := control.GetActionLease(input.ActionID)
					if err != nil || !active || lease.Epoch != input.Epoch || lease.ClaimToken != input.ClaimToken {
						t.Fatal("upgrade retry replaced the original unresolved action lease", err)
					}
					_, _, receipt, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, agentFailureNode(phase))
					if err != nil || !found || receipt == nil {
						t.Fatal("upgrade retry lost the original independently signed outcome", err)
					}
				})
			}
		})
	}
}
