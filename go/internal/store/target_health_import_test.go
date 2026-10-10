package store

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func healthCheckpoint(t *testing.T, empty bool) *AuthorityCheckpoint {
	t.Helper()
	directory, name := "target-health-v1", "python_inventory_checkpoint_v1.json"
	if empty {
		directory, name = "target-health-empty-v1", "python_empty_inventory_checkpoint_v1.json"
	}
	raw, err := os.ReadFile(filepath.Join("testdata", directory, name))
	if err != nil {
		t.Fatal(err)
	}
	var checkpoint AuthorityCheckpoint
	if err := json.Unmarshal(raw, &checkpoint); err != nil {
		t.Fatal(err)
	}
	return &checkpoint
}

func copiedTargetHealthSource(t *testing.T, empty bool) (string, string, []byte) {
	t.Helper()
	directory, prefix := "target-health-v1", "python_"
	if empty {
		directory, prefix = "target-health-empty-v1", "python_empty_"
	}
	read := func(name string) []byte {
		raw, err := os.ReadFile(filepath.Join("testdata", directory, prefix+name))
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	projection := filepath.Join(directory, "python_projection_targets")
	if empty {
		projection = ""
	}
	path, raw := materializePythonSource(t, projection, "target",
		read("control_source_v1.sqlite3"), read("target_inventory_export_v2.json"))
	healthDir := filepath.Join(filepath.Dir(filepath.Dir(path)), ".backup-scheduler")
	if err := os.MkdirAll(healthDir, 0o700); err != nil {
		t.Fatal(err)
	}
	healthPath := filepath.Join(healthDir, "scheduler.db")
	if err := os.WriteFile(healthPath, read("scheduler_source_v1.sqlite3"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path, healthPath, raw
}

func TestTargetInventoryParserRetainsPythonSchedulerHealth(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "target-health-v1", "python_target_inventory_export_v2.json"))
	if err != nil {
		t.Fatal(err)
	}
	manifest, records, err := parsePythonInventoryExport(raw)
	if err != nil || manifest.Schema != "python-control-inventory-export-v2" || len(records) != 1 {
		t.Fatalf("real Python target/health export refused: schema=%s records=%d error=%v", manifest.Schema, len(records), err)
	}
}

func TestTargetHealthAttestationRefusesMissingSchedulerAndLostFence(t *testing.T) {
	for _, fault := range []string{"missing", "lost fence", "changed row"} {
		t.Run(fault, func(t *testing.T) {
			path, healthPath, raw := copiedTargetHealthSource(t, false)
			if _, err := AttestPythonInventorySource(path, raw); err != nil {
				t.Fatalf("valid two-source attestation: %v", err)
			}
			if fault == "missing" {
				if err := os.Remove(healthPath); err != nil {
					t.Fatal(err)
				}
			} else {
				db := openPythonSourceFixtureWriter(t, healthPath)
				if fault == "lost fence" {
					sourceFixtureExec(t, db, "DROP TRIGGER native_target_health_no_update")
				} else {
					tamperWithFencedSource(t, db, "native_target_health_no_update", func() {
						sourceFixtureExec(t, db, "UPDATE backup_target_health SET status='healthy' WHERE target_id='t-1'")
					})
				}
			}
			if _, err := AttestPythonInventorySource(path, raw); err == nil {
				t.Fatal("target attested without its unchanged fenced scheduler source")
			}
		})
	}
}

func TestTargetHealthCannotImportWithoutSourceAttestation(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	_, _, raw := copiedTargetHealthSource(t, false)
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("unattested health import: %v", err)
	}
}

func TestTargetHealthImportPersistsHistoryAndRefusesSourceDriftAtPromotion(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint := healthCheckpoint(t, false)
	if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "target")
	path, healthPath, raw := copiedTargetHealthSource(t, false)
	attested, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	imported, err := control.ImportAttestedPythonInventory(raw, attested)
	if err != nil {
		t.Fatal(err)
	}
	var status, detail string
	if err := control.db.QueryRow("SELECT status,detail FROM backup_target_health WHERE target_id='t-1'").Scan(&status, &detail); err != nil ||
		status != "blocked" || detail != "provider timeout" {
		t.Fatalf("native health history lost: %q %q %v", status, detail, err)
	}
	db := openPythonSourceFixtureWriter(t, healthPath)
	tamperWithFencedSource(t, db, "native_target_health_no_update", func() {
		sourceFixtureExec(t, db, "UPDATE backup_target_health SET status='healthy' WHERE target_id='t-1'")
	})
	if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("changed scheduler source promoted: %v", err)
	}
	if cutover, err := control.GetCutover("target"); err != nil || cutover.State != CutoverDualEvaluate {
		t.Fatalf("refused promotion changed ownership: %+v %v", cutover, err)
	}
}

func importedHealthStore(t *testing.T, empty bool) (*Control, *AuthorityCheckpoint, CutoverRecord, PythonInventoryImportResult) {
	t.Helper()
	control := openAuthority(t)
	checkpoint := healthCheckpoint(t, empty)
	if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "target")
	path, _, raw := copiedTargetHealthSource(t, empty)
	attested, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	imported, err := control.ImportAttestedPythonInventory(raw, attested)
	if err != nil {
		t.Fatal(err)
	}
	return control, checkpoint, dual, imported
}

func TestTargetHealthRestartKeepsHistoryWithoutReadingRetiredSource(t *testing.T) {
	control, checkpoint, dual, imported := importedHealthStore(t, false)
	path := control.path
	if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil {
		t.Fatal(err)
	}
	var sourcePath string
	if err := control.db.QueryRow("SELECT scheduler_source_path FROM control_target_health_imports").Scan(&sourcePath); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(sourcePath); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "health-restarted", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic, FleetID: "fleet-a", Environment: "production"})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	targets, health, err := reopened.ListAuthoritativeTargets()
	if err != nil || len(targets) != 1 || len(health) != 2 || health[0].Detail != nil || health[1].Status != "blocked" {
		t.Fatalf("restarted health snapshot lost: targets=%v health=%v error=%v", targets, health, err)
	}
}

func TestTargetHealthHandbackRemovesBothSnapshotsAtomically(t *testing.T) {
	control, _, _, imported := importedHealthStore(t, false)
	defer control.Close()
	handback, err := control.RollbackPythonInventory("target", imported.TransferID)
	if err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"targets", "backup_target_health", "control_target_health_imports"} {
		var count int
		if err := control.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("handback left native %s: %d %v", table, count, err)
		}
	}
	document, err := CanonicalInventoryHandback(handback)
	if err != nil {
		t.Fatal(err)
	}
	document = append(document, '\n')
	fixture := filepath.Join("testdata", "target-health-v1", "go_target_inventory_handback_v1.json")
	if os.Getenv("DEEPSEEK_UPDATE_TARGET_HEALTH_HANDBACK_FIXTURE") == "1" {
		if err := os.WriteFile(fixture, document, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(fixture)
	if err != nil || string(raw) != string(document) {
		t.Fatalf("Go target/health handback fixture differs: %v", err)
	}
	if cutover, err := control.GetCutover("target"); err != nil || cutover.State != CutoverDualEvaluate {
		t.Fatalf("handback invalidated the store: %+v %v", cutover, err)
	}
}

func TestTargetHealthCorruptionCannotAppearAsValidEmptyHistory(t *testing.T) {
	for _, fault := range []string{"missing history", "changed history", "missing provenance", "changed provenance"} {
		t.Run(fault, func(t *testing.T) {
			control, checkpoint, dual, imported := importedHealthStore(t, false)
			defer control.Close()
			if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); err != nil {
				t.Fatal(err)
			}
			trigger, sql := "backup_target_health_no_delete", "DELETE FROM backup_target_health"
			if fault == "changed history" {
				trigger, sql = "backup_target_health_no_update", "UPDATE backup_target_health SET status='healthy'"
			} else if fault == "missing provenance" {
				trigger, sql = "control_target_health_imports_no_delete", "DELETE FROM control_target_health_imports"
			} else if fault == "changed provenance" {
				trigger, sql = "control_target_health_imports_no_update", "UPDATE control_target_health_imports SET row_count=row_count+1"
			}
			tamperWithFencedSource(t, control.db, trigger, func() { sourceFixtureExec(t, control.db, sql) })
			if _, _, err := control.ListAuthoritativeTargets(); !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("damaged health history accepted: %v", err)
			}
		})
	}
}
