package store

import (
	"database/sql"
	"errors"
	"path/filepath"
	"reflect"
	"testing"
)

func TestTargetRegistrationMissingOperatorOrMalformedEventCannotPublishHealthyState(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "target")
	scope, receipt, document := registrationFixture(t, control, "target-inspect-original-journal", "target_original", path)
	original, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
	if err != nil {
		t.Fatal(err)
	}
	withRegistrationRollback(t, control, func(tx *sql.Tx) {
		rows, err := tx.Query("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='control_operator_mutations'")
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
			_ = rows.Close()
			t.Fatal(err)
		}
		if err := rows.Close(); err != nil {
			t.Fatal(err)
		}
		for _, name := range names {
			if _, err := tx.Exec(`DROP TRIGGER "` + name + `"`); err != nil {
				t.Fatal(err)
			}
		}
		if _, err := tx.Exec("DELETE FROM control_operator_mutations WHERE operation_id=?", scope.operationID); err != nil {
			t.Fatal(err)
		}
		if _, err := loadTargetRegistrationBindingsTx(tx, original.ID); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("a signed witness without its original operation result looked healthy: %v", err)
		}
	})
	after, exists, err := control.Get("target", original.ID)
	if err != nil || !exists || !reflect.DeepEqual(after, original) {
		t.Fatal("rollback failed to retain the original target and operator result")
	}
	next, nextReceipt, nextDocument := registrationFixture(t, control, "target-inspect-malformed-event", "target_original", path)
	fault := inventoryReadFault{query: "SELECT DISTINCT record_id FROM control_events WHERE domain='target'", malformed: true}
	installInventoryReadFault(t, control, &fault)
	fault.armed = true
	if _, _, err := control.RegisterInspectedBackupTarget(next, nextReceipt, nextDocument); err == nil || !fault.injected {
		t.Fatalf("an undecodable original event id produced a healthy catalog: %v injected=%v", err, fault.injected)
	}
	after, exists, err = control.Get("target", original.ID)
	if err != nil || !exists || !reflect.DeepEqual(after, original) {
		t.Fatal("malformed SQLite event row changed the original target")
	}
	recovered, _, err := control.RegisterInspectedBackupTarget(next, nextReceipt, nextDocument)
	if err != nil || recovered.Revision != original.Revision+1 {
		t.Fatalf("original request failed after a proven read rollback: %+v %v", recovered, err)
	}
}
