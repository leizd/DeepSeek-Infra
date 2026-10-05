package store

import (
	"errors"
	"reflect"
	"testing"
)

func TestInventoryHandbackCanBeRepublishedWithoutRepeatingOwnershipChanges(t *testing.T) {
	control, _, _, imported := importedHealthStore(t, false)
	defer control.Close()
	committed, err := control.RollbackPythonInventory("target", imported.TransferID)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		read, err := control.ReadPythonInventoryHandback("target", imported.TransferID)
		if err != nil || !reflect.DeepEqual(read, committed) {
			t.Fatalf("lost-publication recovery changed committed proof: %+v %v", read, err)
		}
	}
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_inventory_handbacks").Scan(&count); err != nil || count != 1 {
		t.Fatalf("republication duplicated a durable handback: %d %v", count, err)
	}
	if _, err := control.RollbackPythonInventory("target", imported.TransferID); !errors.Is(err, ErrInventoryHandbackConflict) {
		t.Fatalf("republication made a second rollback possible: %v", err)
	}
}

func TestHandbackRepublicationRefusesMissingDamagedAndInactiveProofs(t *testing.T) {
	for _, fault := range []string{"missing", "bad domain", "bad transfer", "inactive", "closed", "closed database", "missing guard", "changed digest"} {
		t.Run(fault, func(t *testing.T) {
			control, _, result := handbackFixture(t, "policy")
			defer control.Close()
			if _, err := control.RollbackPythonInventory("policy", result.TransferID); err != nil {
				t.Fatal(err)
			}
			domain, transfer := "policy", result.TransferID
			switch fault {
			case "missing":
				transfer = "missing-transfer"
			case "bad domain":
				domain = "action"
			case "bad transfer":
				transfer = "../escape"
			case "inactive":
				control.schema = 0
			case "closed":
				if err := control.Close(); err != nil {
					t.Fatal(err)
				}
			case "closed database":
				if err := control.db.Close(); err != nil {
					t.Fatal(err)
				}
			case "missing guard":
				if _, err := control.db.Exec("DROP TRIGGER control_inventory_handbacks_no_delete"); err != nil {
					t.Fatal(err)
				}
			case "changed digest":
				for _, statement := range []string{
					"DROP TRIGGER control_inventory_handbacks_no_update",
					"UPDATE control_inventory_handbacks SET handback_digest='" + repeatHex("f") + "'",
					inventoryHandbackSchemaObjects["control_inventory_handbacks_no_update"],
				} {
					if _, err := control.db.Exec(statement); err != nil {
						t.Fatal(err)
					}
				}
			}
			if _, err := control.ReadPythonInventoryHandback(domain, transfer); err == nil {
				t.Fatal("an unverifiable handback was republished")
			}
		})
	}
}
