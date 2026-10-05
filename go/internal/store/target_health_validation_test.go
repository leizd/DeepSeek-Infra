package store

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestTargetHealthManifestRejectsMalformedAndRehashedRows(t *testing.T) {
	_, _, raw := copiedTargetHealthSource(t, false)
	mutations := map[string]func(map[string]any){
		"v1 cannot carry health":            func(d map[string]any) { d["schema"] = PythonInventoryExportSchema },
		"policy cannot carry target health": func(d map[string]any) { d["domain"] = "policy" },
		"missing binding":                   func(d map[string]any) { delete(d, "targetHealth") },
		"unexpected binding":                func(d map[string]any) { d["other"] = true },
		"non-object binding":                func(d map[string]any) { d["targetHealth"] = nil },
		"wrong health version":              func(d map[string]any) { d["targetHealth"].(map[string]any)["schema"] = "v0" },
		"extra health field":                func(d map[string]any) { d["targetHealth"].(map[string]any)["other"] = true },
		"invalid digest":                    func(d map[string]any) { d["targetHealth"].(map[string]any)["sourceDigest"] = "bad" },
		"changed digest":                    func(d map[string]any) { d["targetHealth"].(map[string]any)["sourceDigest"] = repeatHex("b") },
		"null row set":                      func(d map[string]any) { d["targetHealth"].(map[string]any)["rows"] = nil },
	}
	for name, change := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := resealInventoryFixture(t, raw, change)
			if _, _, err := parsePythonInventoryExport(changed); !errors.Is(err, ErrInventoryImportInvalid) {
				t.Fatalf("invalid target health binding passed: %v", err)
			}
		})
	}
	rowMutations := map[string]func([]any){
		"non-object row":               func(rows []any) { rows[0] = "old-target" },
		"missing field":                func(rows []any) { delete(rows[0].(map[string]any), "detail") },
		"wrong field":                  func(rows []any) { r := rows[0].(map[string]any); delete(r, "detail"); r["other"] = nil },
		"duplicate target":             func(rows []any) { rows[1] = rows[0] },
		"unsorted targets":             func(rows []any) { rows[0], rows[1] = rows[1], rows[0] },
		"path target":                  func(rows []any) { rows[0].(map[string]any)["target_id"] = "../escape" },
		"dot target":                   func(rows []any) { rows[0].(map[string]any)["target_id"] = "." },
		"unsupported target character": func(rows []any) { rows[0].(map[string]any)["target_id"] = "bad id" },
		"numeric status":               func(rows []any) { rows[0].(map[string]any)["status"] = 1 },
		"empty status":                 func(rows []any) { rows[0].(map[string]any)["status"] = "" },
		"empty check time":             func(rows []any) { rows[0].(map[string]any)["checked_at"] = "" },
		"object detail":                func(rows []any) { rows[0].(map[string]any)["detail"] = map[string]any{} },
	}
	for name, change := range rowMutations {
		t.Run(name, func(t *testing.T) {
			changed := resealInventoryFixture(t, raw, func(d map[string]any) {
				health := d["targetHealth"].(map[string]any)
				change(health["rows"].([]any))
				digest, err := hashCanonicalJSON(health["rows"])
				if err != nil {
					t.Fatal(err)
				}
				health["sourceDigest"] = digest
			})
			if _, _, err := parsePythonInventoryExport(changed); !errors.Is(err, ErrInventoryImportInvalid) {
				t.Fatalf("rehashed invalid target health row passed: %v", err)
			}
		})
	}
}

func TestTargetHealthSourceFenceBindsEverySourceIdentity(t *testing.T) {
	for _, column := range []string{"transfer_id", "authority_digest", "target_source_digest", "health_digest"} {
		t.Run(column, func(t *testing.T) {
			path, scheduler, raw := copiedTargetHealthSource(t, false)
			db := openPythonSourceFixtureWriter(t, scheduler)
			tamperWithFencedSource(t, db, "native_target_health_fence_no_update", func() {
				sourceFixtureExec(t, db, "UPDATE native_target_health_handoff_fence SET "+column+"='different'")
			})
			if _, err := AttestPythonInventorySource(path, raw); !errors.Is(err, ErrPythonSourceFenceInvalid) {
				t.Fatalf("scheduler fence %s mismatch accepted: %v", column, err)
			}
		})
	}
}

func TestTargetHealthCustomSourcePathSurvivesRestartAndReattestation(t *testing.T) {
	path, scheduler, raw := copiedTargetHealthSource(t, false)
	custom := filepath.Join(t.TempDir(), "fenced-scheduler-copy.sqlite3")
	if err := os.Rename(scheduler, custom); err != nil {
		t.Fatal(err)
	}
	for _, supplied := range []string{"", "relative.sqlite3", path, filepath.Dir(custom)} {
		if _, err := AttestPythonInventorySources(path, "", supplied, raw); !errors.Is(err, ErrPythonSourceFenceInvalid) {
			t.Fatalf("invalid scheduler source %q accepted: %v", supplied, err)
		}
	}
	attested, err := AttestPythonInventorySources(path, "", custom, raw)
	if err != nil {
		t.Fatal(err)
	}
	control := openAuthority(t)
	checkpoint := healthCheckpoint(t, false)
	if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "target")
	imported, err := control.ImportAttestedPythonInventory(raw, attested)
	if err != nil {
		t.Fatal(err)
	}
	goPath := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	control, err = OpenControl(OpenOptions{Path: goPath, Owner: "health-custom-restarted", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic, FleetID: "fleet-a", Environment: "production"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil {
		t.Fatalf("persisted explicit scheduler path was not reattested: %v", err)
	}
	_, health, err := control.ListAuthoritativeTargets()
	if err != nil || len(health) != 2 {
		t.Fatalf("restarted health projection: %+v %v", health, err)
	}
}

func TestTargetHealthSourceRefusesChangedSQLTypesAndSchema(t *testing.T) {
	for _, fault := range []string{"blob masquerading as text", "blob detail", "foreign health schema"} {
		t.Run(fault, func(t *testing.T) {
			path, scheduler, raw := copiedTargetHealthSource(t, false)
			db := openPythonSourceFixtureWriter(t, scheduler)
			if fault == "blob masquerading as text" || fault == "blob detail" {
				tamperWithFencedSource(t, db, "native_target_health_no_update", func() {
					query, value := "UPDATE backup_target_health SET status=? WHERE target_id='t-1'", "blocked"
					if fault == "blob detail" {
						query, value = "UPDATE backup_target_health SET detail=? WHERE target_id='t-1'", "provider timeout"
					}
					if _, err := db.Exec(query, []byte(value)); err != nil {
						t.Fatal(err)
					}
				})
			} else {
				for _, name := range []string{"native_target_health_no_insert", "native_target_health_no_update", "native_target_health_no_delete"} {
					sourceFixtureExec(t, db, "DROP TRIGGER "+name)
				}
				sourceFixtureExec(t, db, "ALTER TABLE backup_target_health RENAME TO old_health")
				sourceFixtureExec(t, db, "CREATE TABLE backup_target_health(target_id BLOB PRIMARY KEY,status TEXT NOT NULL,checked_at TEXT NOT NULL,detail TEXT)")
				sourceFixtureExec(t, db, "INSERT INTO backup_target_health SELECT * FROM old_health")
				sourceFixtureExec(t, db, "DROP TABLE old_health")
				for _, name := range []string{"native_target_health_no_insert", "native_target_health_no_update", "native_target_health_no_delete"} {
					sourceFixtureExec(t, db, targetHealthSourceFenceObjects[name])
				}
			}
			if _, err := AttestPythonInventorySource(path, raw); err == nil {
				t.Fatal("a matching displayed string concealed a changed source SQL type or schema")
			}
		})
	}
}

func TestTargetHealthImportReattestsSchedulerBeforeWriting(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, _, err := control.ClaimControlAuthority(healthCheckpoint(t, false)); err != nil {
		t.Fatal(err)
	}
	dualEvaluate(t, control, "target")
	path, scheduler, raw := copiedTargetHealthSource(t, false)
	attested, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	db := openPythonSourceFixtureWriter(t, scheduler)
	tamperWithFencedSource(t, db, "native_target_health_no_update", func() {
		sourceFixtureExec(t, db, "UPDATE backup_target_health SET detail='changed after attestation' WHERE target_id='t-1'")
	})
	if _, err := control.ImportAttestedPythonInventory(raw, attested); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("scheduler changed between attestation and import: %v", err)
	}
	for _, table := range []string{"targets", "backup_target_health", "control_target_health_imports", "control_inventory_imports"} {
		var count int
		if err := control.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("refused import wrote %s: count=%d error=%v", table, count, err)
		}
	}
}

func TestTargetHealthCannotAttestAnUnreadableOrUnfencedScheduler(t *testing.T) {
	for _, fault := range []string{"invalid database", "missing fence row"} {
		t.Run(fault, func(t *testing.T) {
			path, scheduler, raw := copiedTargetHealthSource(t, false)
			if fault == "invalid database" {
				if err := os.WriteFile(scheduler, []byte("not a SQLite database"), 0o600); err != nil {
					t.Fatal(err)
				}
			} else {
				db := openPythonSourceFixtureWriter(t, scheduler)
				tamperWithFencedSource(t, db, "native_target_health_fence_no_delete", func() {
					sourceFixtureExec(t, db, "DELETE FROM native_target_health_handoff_fence")
				})
			}
			if _, err := AttestPythonInventorySource(path, raw); !errors.Is(err, ErrPythonSourceFenceInvalid) {
				t.Fatalf("unfenced or unreadable scheduler passed: %v", err)
			}
		})
	}
}

func TestTargetHealthNonstandardLayoutRequiresBothExplicitSources(t *testing.T) {
	path, scheduler, raw := copiedTargetHealthSource(t, false)
	projection := filepath.Join(filepath.Dir(filepath.Dir(path)), ".backup-targets")
	custom := filepath.Join(t.TempDir(), "fenced-control-copy.sqlite3")
	if err := os.Rename(path, custom); err != nil {
		t.Fatal(err)
	}
	if _, err := AttestPythonInventorySources(custom, projection, "", raw); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("a nonstandard source guessed its scheduler path: %v", err)
	}
	if _, err := AttestPythonInventorySources(custom, projection, scheduler, raw); err != nil {
		t.Fatalf("explicit sources and original projection refused: %v", err)
	}
	oldSource, oldRaw := copiedPythonSourceFixture(t, "target")
	if _, err := AttestPythonInventorySources(oldSource, "", scheduler, oldRaw); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("v1 import silently accepted an unbound health source: %v", err)
	}
}

func TestAuthoritativeTargetStoreCannotInventHealthForAV1Import(t *testing.T) {
	control, checkpoint, imported := handbackFixture(t, "target")
	defer control.Close()
	dual, err := control.GetCutover("target")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.ListAuthoritativeTargets(); !errors.Is(err, ErrTargetHealthNotTransferred) {
		t.Fatalf("registry-only promotion fabricated an attested empty health source: %v", err)
	}
}
