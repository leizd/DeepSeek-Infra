package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
)

// Historical shapes exist only in disposable fixtures. The application has no
// downgrade path, and this helper cannot copy a v17 confirmed failure into v16.
func materializeFailureV16Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, statement := range []string{
		`CREATE TEMP TABLE failure_lease_v16_fixture AS SELECT * FROM action_leases`,
		`DROP TABLE action_leases`, actionCancellationLeaseSchema,
		`INSERT INTO action_leases SELECT * FROM failure_lease_v16_fixture`,
		`DROP TABLE failure_lease_v16_fixture`,
		`CREATE TABLE failure_meta_v16_fixture(singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 16),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO failure_meta_v16_fixture SELECT singleton,runtime,mode,16,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE failure_meta_v16_fixture RENAME TO control_store_meta`,
		`DELETE FROM schema_migrations WHERE version>=17`, `PRAGMA user_version=16`,
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV16); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV16
}

// Compare every durable row, including authority witnesses, request custody and
// unrelated domains. Only the two deliberately advanced version tables differ.
func failureMigrationRows(t *testing.T, db *sql.DB) string {
	t.Helper()
	rows, err := db.Query(`SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('schema_migrations','control_store_meta') ORDER BY name`)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			t.Fatal(err)
		}
		names = append(names, name)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	_ = rows.Close()
	all := make(map[string][][]any)
	for _, name := range names {
		rows, err := db.Query(`SELECT * FROM "` + strings.ReplaceAll(name, `"`, `""`) + `" ORDER BY rowid`)
		if err != nil {
			t.Fatal(err)
		}
		columns, err := rows.Columns()
		if err != nil {
			t.Fatal(err)
		}
		all[name] = nil
		for rows.Next() {
			values, pointers := make([]any, len(columns)), make([]any, len(columns))
			for i := range values {
				pointers[i] = &values[i]
			}
			if err := rows.Scan(pointers...); err != nil {
				t.Fatal(err)
			}
			all[name] = append(all[name], values)
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

func TestV17FailureMigrationPreservesRowsThenSettlesVerifiedPhase(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		t.Run(phase, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, phase)
			materializeFailureV16Fixture(t, control)
			for _, state := range []string{"SUCCEEDED", "FAILED_BEFORE_EFFECT", "CANCELLED_CONFIRMED"} {
				if _, err := control.db.Exec(`INSERT INTO action_leases SELECT ?,owner,epoch,claim_token,lease_until,acquired_at,updated_at,claim_revision,writer_fencing_token,? FROM action_leases WHERE action_id=?`, "historical-"+state, state, input.ActionID); err != nil {
					t.Fatal(err)
				}
			}
			before := failureMigrationRows(t, control.db)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			if err := control.migrateTx(tx); err != nil {
				_ = tx.Rollback()
				t.Fatal(err)
			}
			if err := tx.Commit(); err != nil {
				t.Fatal(err)
			}
			if control.SchemaVersion() != SchemaV17 || failureMigrationRows(t, control.db) != before {
				t.Fatal("migration changed original leases, custody, witnesses or domain history")
			}
			result, err := control.SettleAgentFailure(input)
			if err != nil || result.Action.State != "FAILED" || result.Run.Status != "failed" {
				t.Fatalf("migrated phase did not settle its original signed evidence: %v", err)
			}
			var terminal string
			if err := control.db.QueryRow(`SELECT terminal_state FROM action_leases WHERE action_id=?`, input.ActionID).Scan(&terminal); err != nil || terminal != "FAILED_CONFIRMED" {
				t.Fatalf("failure lease lost its distinct conclusive terminal: %q %v", terminal, err)
			}
			if _, active, err := control.GetActionLease(input.ActionID); err != nil || active {
				t.Fatal("proved failure remained an active lease", err)
			}
			replay, err := control.SettleAgentFailure(input)
			if err != nil || !replay.AlreadyApplied || !reflect.DeepEqual(result.Run, replay.Run) {
				t.Fatal("migrated failure could not replay its original cursor", err)
			}
		})
	}
}

func TestV16StoreOpenMigratesFailureLeaseAndPreservesOriginalCustody(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	materializeFailureV16Fixture(t, control)
	path, now := control.path, control.now
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "failure-v17-reopen", Now: now, AuthorizeCutover: true,
		PromotionSignerPublicKey: control.promotionSignerKey, AgentOutcomeSignerPublicKey: control.agentOutcomeSignerKey,
		FleetID: control.fleetID, Environment: control.environment})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	if reopened.SchemaVersion() != SchemaV17 {
		t.Fatal("open did not advance historical metadata, journal and user_version together")
	}
	lease, active, err := reopened.GetActionLease(input.ActionID)
	if err != nil || !active || lease.Epoch != input.Epoch || lease.ClaimToken != input.ClaimToken {
		t.Fatal("opening v16 changed the original unresolved lease", err)
	}
	_, _, receipt, found, err := reopened.AgentNodeOutcomeSnapshot(input.ActionID, agentFailureNode("plan"))
	if err != nil || !found || receipt == nil {
		t.Fatal("opening v16 lost retained independently signed evidence", err)
	}
}

func TestV17FailureMigrationFaultsRollBackRowsVersionsAndSourceShape(t *testing.T) {
	for _, fault := range []string{"metadata collision", "lease collision", "copy constraint", "negative clock", "late journal", "foreign source"} {
		t.Run(fault, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			materializeFailureV16Fixture(t, control)
			switch fault {
			case "metadata collision":
				if _, err := control.db.Exec(`CREATE TABLE control_store_meta_v17(x INTEGER)`); err != nil {
					t.Fatal(err)
				}
			case "lease collision":
				if _, err := control.db.Exec(`CREATE TABLE action_leases_v17(x INTEGER)`); err != nil {
					t.Fatal(err)
				}
			case "copy constraint":
				for _, statement := range []string{`PRAGMA ignore_check_constraints=ON`, `UPDATE action_leases SET terminal_state='FAILED' WHERE action_id='` + input.ActionID + `'`, `PRAGMA ignore_check_constraints=OFF`} {
					if _, err := control.db.Exec(statement); err != nil {
						t.Fatal(err)
					}
				}
			case "negative clock":
				control.now = func() int64 { return -1 }
			case "late journal":
				if _, err := control.db.Exec(`CREATE TEMP TRIGGER failure_migration_v17_fault BEFORE INSERT ON schema_migrations WHEN NEW.version=17 BEGIN SELECT RAISE(ABORT,'failure-migration-v17-fault'); END`); err != nil {
					t.Fatal(err)
				}
			case "foreign source":
				if _, err := control.db.Exec(`DROP TRIGGER action_lease_events_no_update`); err != nil {
					t.Fatal(err)
				}
			}
			before := failureMigrationRows(t, control.db)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.migrateToV17Tx(tx)
			if err == nil || fault == "negative clock" && !errors.Is(err, ErrWriterFenceHeld) || fault == "foreign source" && !errors.Is(err, ErrForeignRuntimeStore) {
				t.Fatalf("migration failed to reject %s: %v", fault, err)
			}
			_ = tx.Rollback()
			if control.SchemaVersion() != SchemaV16 || failureMigrationRows(t, control.db) != before {
				t.Fatal("failed migration changed durable rows or in-memory schema")
			}
			var meta, version, count int
			if err := control.db.QueryRow(`SELECT schema_version FROM control_store_meta`).Scan(&meta); err != nil {
				t.Fatal(err)
			}
			if err := control.db.QueryRow(`PRAGMA user_version`).Scan(&version); err != nil {
				t.Fatal(err)
			}
			if err := control.db.QueryRow(`SELECT count(*) FROM schema_migrations`).Scan(&count); err != nil || meta != 16 || version != 16 || count != 16 {
				t.Fatal("failed migration partially advanced metadata or migration history", err)
			}
			if fault != "foreign source" {
				tx, err = control.db.Begin()
				if err != nil {
					t.Fatal(err)
				}
				defer tx.Rollback()
				if err := verifySchemaTx(tx, SchemaV16); err != nil {
					t.Fatal("rollback lost the exact historical schema", err)
				}
			}
		})
	}
}

func TestV17FailureTerminalCannotBypassTypedProofSettlement(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	before := failureMigrationRows(t, control.db)
	if _, err := control.transitionLeasedAction(input.ActionID, input.Epoch, input.ClaimToken, "FAILED_CONFIRMED", nil); err == nil {
		t.Fatal("generic transition admitted typed failure terminal")
	}
	for _, unproved := range []string{"FAILED", "EFFECT_UNKNOWN"} {
		if _, err := control.db.Exec(`UPDATE action_leases SET terminal_state=? WHERE action_id=?`, unproved, input.ActionID); err == nil {
			t.Fatal("lease terminal CHECK admitted an unresolved or untyped failure", unproved)
		}
	}
	if failureMigrationRows(t, control.db) != before {
		t.Fatal("unproved failure changed custody or released resources")
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec(`DROP TRIGGER action_lease_events_no_delete`); err != nil {
		t.Fatal(err)
	}
	if err := verifySchemaTx(tx, SchemaV17); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatal("v17 verification accepted missing immutable history guard", err)
	}
}
