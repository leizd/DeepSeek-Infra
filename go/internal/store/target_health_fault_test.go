package store

import (
	"errors"
	"testing"
)

func TestTargetHealthImportRollsBackEverySnapshotOnDurableWriteFailure(t *testing.T) {
	for _, table := range []string{"backup_target_health", "control_target_health_imports"} {
		t.Run(table, func(t *testing.T) {
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
			if _, err := control.db.Exec("CREATE TRIGGER fail_health_insert BEFORE INSERT ON " + table + " BEGIN SELECT RAISE(ABORT,'health write failed'); END"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.ImportAttestedPythonInventory(raw, attested); err == nil {
				t.Fatal("failed health write committed")
			}
			for _, checked := range []string{"targets", "control_events", "control_inventory_imports", "backup_target_health", "control_target_health_imports"} {
				var count int
				if err := control.db.QueryRow("SELECT COUNT(*) FROM " + checked).Scan(&count); err != nil || count != 0 {
					t.Fatalf("failed health write retained %s: %d %v", checked, count, err)
				}
			}
			if _, err := control.db.Exec("DROP TRIGGER fail_health_insert"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.ImportAttestedPythonInventory(raw, attested); err != nil {
				t.Fatalf("clean retry after rollback: %v", err)
			}
		})
	}
}

func TestTargetHealthHandbackKeepsBothOwnersFencedOnDurableDeleteFailure(t *testing.T) {
	for _, table := range []string{"backup_target_health", "control_target_health_imports"} {
		t.Run(table, func(t *testing.T) {
			control, _, _, imported := importedHealthStore(t, false)
			defer control.Close()
			if _, err := control.db.Exec("CREATE TRIGGER fail_health_delete BEFORE DELETE ON " + table + " BEGIN SELECT RAISE(ABORT,'health delete failed'); END"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.RollbackPythonInventory("target", imported.TransferID); err == nil {
				t.Fatal("failed health delete committed")
			}
			for table, wanted := range map[string]int{"targets": 1, "control_inventory_imports": 1, "backup_target_health": 2, "control_target_health_imports": 1, "control_inventory_handbacks": 0} {
				var count int
				if err := control.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != wanted {
					t.Fatalf("partial handback %s: count=%d wanted=%d error=%v", table, count, wanted, err)
				}
			}
			if _, err := control.GetCutover("target"); err != nil {
				t.Fatalf("rollback lost lifted health guards: %v", err)
			}
			if _, err := control.ReadPythonInventoryHandback("target", imported.TransferID); !errors.Is(err, ErrInventoryHandbackNotFound) {
				t.Fatalf("uncommitted handback proof published: %v", err)
			}
			if _, err := control.db.Exec("DROP TRIGGER fail_health_delete"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.RollbackPythonInventory("target", imported.TransferID); err != nil {
				t.Fatalf("clean handback retry: %v", err)
			}
		})
	}
}
