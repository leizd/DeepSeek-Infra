package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"testing"
)

func nativeRecoveryCatalog(t *testing.T, db *sql.DB) string {
	t.Helper()
	rows, err := db.Query("SELECT type,name,tbl_name,sql FROM main.sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var catalog [][4]string
	for rows.Next() {
		var row [4]string
		if err := rows.Scan(&row[0], &row[1], &row[2], &row[3]); err != nil {
			t.Fatal(err)
		}
		catalog = append(catalog, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(catalog)
	if err != nil {
		t.Fatal(err)
	}
	return string(encoded)
}

func TestNativeControlRollbackStorageFaultKeepsOriginalSchema(t *testing.T) {
	faults := []struct{ kind, table string }{
		{"missing", "action_verification_boundary"},
		{"missing", "backup_target_health"},
		{"missing", "control_cutover_authorizations"},
		{"missing", "control_inventory_imports"},
		{"missing", "control_inventory_handbacks"},
		{"missing", "control_promotion_artifacts"},
		{"missing", "control_authority_head"},
		{"missing", "action_reconciliation_boundary"},
		{"drop-refused", "backup_target_health"},
		{"drop-refused", operatorMutationTable},
		{"drop-refused", "control_inventory_imports"},
		{"drop-refused", "control_inventory_handbacks"},
		{"drop-refused", "control_operations"},
		{"drop-refused", "control_cutover_events"},
		{"drop-refused", "control_cutover"},
		{"deferred-commit", "control_store_meta"},
	}
	for _, fault := range faults {
		t.Run(fault.kind+"/"+fault.table, func(t *testing.T) {
			control, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "legacy-rollback-fault", Now: func() int64 { return 1000 }})
			if err != nil {
				t.Fatal(err)
			}
			defer control.Close()
			if fault.kind == "missing" {
				if _, err := control.db.Exec("DROP TABLE " + fault.table); err != nil {
					t.Fatal(err)
				}
			}
			before := failureMigrationRows(t, control.db)
			versions, catalog := nativeV18VersionRows(t, control.db), nativeRecoveryCatalog(t, control.db)
			if fault.kind == "drop-refused" {
				if _, err := control.db.Exec("CREATE TEMP VIEW " + fault.table + " AS SELECT * FROM main." + fault.table); err != nil {
					t.Fatal(err)
				}
			} else if fault.kind == "deferred-commit" {
				for _, statement := range []string{
					"CREATE TEMP TABLE native_rollback_parent(id INTEGER PRIMARY KEY)",
					"CREATE TEMP TABLE native_rollback_child(id INTEGER REFERENCES native_rollback_parent(id) DEFERRABLE INITIALLY DEFERRED)",
					"CREATE TEMP TRIGGER native_rollback_commit_fault AFTER UPDATE ON control_store_meta WHEN NEW.schema_version=0 BEGIN INSERT INTO native_rollback_child VALUES(1); END",
				} {
					if _, err := control.db.Exec(statement); err != nil {
						t.Fatal(err)
					}
				}
			}
			if err := control.Rollback(0); err == nil {
				t.Fatal("unavailable schema or failed commit was treated as a complete rollback")
			}
			if fault.kind == "drop-refused" {
				if _, err := control.db.Exec("DROP VIEW temp." + fault.table); err != nil {
					t.Fatal(err)
				}
			} else if fault.kind == "deferred-commit" {
				for _, statement := range []string{"DROP TRIGGER temp.native_rollback_commit_fault", "DROP TABLE temp.native_rollback_child", "DROP TABLE temp.native_rollback_parent"} {
					if _, err := control.db.Exec(statement); err != nil {
						t.Fatal(err)
					}
				}
			}
			if control.SchemaVersion() != CurrentSchema || failureMigrationRows(t, control.db) != before ||
				nativeV18VersionRows(t, control.db) != versions || nativeRecoveryCatalog(t, control.db) != catalog {
				t.Fatal("failed rollback partially dropped original tables, immutability guards, rows or version history")
			}
			if fault.kind != "missing" {
				if err := control.Rollback(0); err != nil || control.SchemaVersion() != 0 {
					t.Fatal("removing only the temporary fault prevented the original empty-store rollback", err)
				}
			}
		})
	}
}

func TestNativeControlRollbackRetainsImportedHealthHistory(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, _, err := control.ClaimControlAuthority(healthCheckpoint(t, false)); err != nil {
		t.Fatal(err)
	}
	dualEvaluate(t, control, "target")
	path, _, raw := copiedTargetHealthSource(t, false)
	attested, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = control.ImportAttestedPythonInventory(raw, attested); err != nil {
		t.Fatal(err)
	}
	var count int
	if err = control.db.QueryRow("SELECT COUNT(*) FROM backup_target_health").Scan(&count); err != nil || count == 0 {
		t.Fatal("fixture did not retain actual imported target health", err)
	}
	before, versions, catalog := failureMigrationRows(t, control.db), nativeV18VersionRows(t, control.db), nativeRecoveryCatalog(t, control.db)
	if err = control.Rollback(0); !errors.Is(err, ErrInventoryHistoryRetained) {
		t.Fatal("rollback did not refuse the retained imported health history", err)
	}
	if control.SchemaVersion() != CurrentSchema || failureMigrationRows(t, control.db) != before ||
		nativeV18VersionRows(t, control.db) != versions || nativeRecoveryCatalog(t, control.db) != catalog {
		t.Fatal("history refusal dropped original provenance, inventory or control schema")
	}
}

func nativeSettlementReadFixture(t *testing.T, phase string) (*Control, func() (bool, error)) {
	t.Helper()
	switch phase {
	case "plan":
		control, input, _ := planSettlementFixture(t, false)
		return control, func() (bool, error) { result, err := control.SettleAgentPlan(input); return result.AlreadyApplied, err }
	case "task":
		control, input, _ := taskSettlementFixture(t, false)
		return control, func() (bool, error) { result, err := control.SettleAgentTask(input); return result.AlreadyApplied, err }
	case "synthesis":
		control, input, _ := synthesisSettlementFixture(t)
		return control, func() (bool, error) {
			result, err := control.SettleAgentSynthesis(input)
			return result.AlreadyApplied, err
		}
	case "failure-plan", "failure-synthesis":
		sourcePhase := "plan"
		if phase == "failure-synthesis" {
			sourcePhase = "synthesis"
		}
		control, input, _ := failureSettlementFixture(t, sourcePhase)
		return control, func() (bool, error) {
			result, err := control.SettleAgentFailure(input)
			return result.AlreadyApplied, err
		}
	default:
		t.Fatal("unknown settlement fixture")
		return nil, nil
	}
}

func TestNativeAgentSettlementReadFaultRetainsOriginalProof(t *testing.T) {
	for _, phase := range []string{"plan", "task", "synthesis", "failure-plan", "failure-synthesis"} {
		t.Run(phase, func(t *testing.T) {
			for _, table := range []string{"control_cutover", "action_lease_events"} {
				t.Run(table, func(t *testing.T) {
					control, settle := nativeSettlementReadFixture(t, phase)
					defer control.Close()
					before, versions := failureMigrationRows(t, control.db), nativeV18VersionRows(t, control.db)
					if _, err := control.db.Exec("CREATE TEMP VIEW " + table + " AS SELECT * FROM main." + table + " WHERE json_extract('broken','$')=1"); err != nil {
						t.Fatal(err)
					}
					if applied, err := settle(); err == nil || applied {
						t.Fatal("unreadable original authority or admission was accepted as settled", err)
					}
					if _, err := control.db.Exec("DROP VIEW temp." + table); err != nil {
						t.Fatal(err)
					}
					if failureMigrationRows(t, control.db) != before || nativeV18VersionRows(t, control.db) != versions {
						t.Fatal("read failure changed original custody, action epoch, writer, events or reservations")
					}
					if replayed, err := settle(); err != nil || replayed {
						t.Fatal("original signed input could not settle after the read fault was removed", err)
					}
					after := failureMigrationRows(t, control.db)
					if replayed, err := settle(); err != nil || !replayed || failureMigrationRows(t, control.db) != after {
						t.Fatal("same settlement did not replay without new durable effects", err)
					}
				})
			}
		})
	}
}
