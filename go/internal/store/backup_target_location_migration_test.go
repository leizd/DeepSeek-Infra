package store

import (
	"errors"
	"path/filepath"
	"reflect"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

func locationV21Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := retireEmptyTargetLocationsTx(tx); err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v21 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 21),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v21 SELECT singleton,runtime,mode,21,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v21 RENAME TO control_store_meta`,
		`DELETE FROM schema_migrations WHERE version=22`, `PRAGMA user_version=21`,
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV21); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV21
}

func TestTargetRegistrationLocationV21UpgradePreservesOriginalBytesAndReplay(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	locationV21Fixture(t, control)
	previous := filepath.Join(t.TempDir(), "old")
	scope, marker, document := registrationFixture(t, control, "target-inspect-pre-location-first", "target_relocated", previous)
	if _, _, err := control.RegisterInspectedBackupTarget(scope, marker, document); err != nil {
		t.Fatal(err)
	}
	scope, marker, document = registrationFixture(t, control, "target-inspect-pre-location-move", "target_relocated", filepath.Join(t.TempDir(), "moved"))
	oldProof := relocationFixture(t, control, previous, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
	if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, oldProof, document); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("v21 silently wrote a v22 extension", err)
	}
	original, result, err := control.RegisterInspectedBackupTarget(scope, marker, document)
	if err != nil {
		t.Fatal(err)
	}
	var boundary int64
	if err := control.db.QueryRow("SELECT COALESCE(MAX(event_id),0) FROM control_events").Scan(&boundary); err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	if recovered.SchemaVersion() != SchemaV22 {
		t.Fatal("v21 did not upgrade to the location-proof layout")
	}
	after, found, err := recovered.Get("target", original.ID)
	if err != nil || !found || !reflect.DeepEqual(original, after) {
		t.Fatal("upgrade reinterpreted the historical target", err)
	}
	var gotBoundary int64
	if err := recovered.db.QueryRow("SELECT last_legacy_event_id FROM control_target_location_boundary WHERE singleton=1").Scan(&gotBoundary); err != nil || gotBoundary != boundary {
		t.Fatal("upgrade did not retain the original event boundary", err)
	}
	replayed, replay, err := recovered.RegisterInspectedBackupTarget(scope, marker, document)
	if err != nil || replay.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(replayed, original) {
		t.Fatal("upgrade revoked old exact replay", err)
	}
	replay.Status = result.Status
	if !reflect.DeepEqual(replay, result) {
		t.Fatal("upgrade changed the historical result identity")
	}
	var count int
	if err := recovered.db.QueryRow("SELECT COUNT(*) FROM control_target_registration_locations").Scan(&count); err != nil || count != 0 {
		t.Fatal("upgrade invented location evidence", err)
	}
	tx, err := recovered.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	wire, _ := proto.MarshalOptions{Deterministic: true}.Marshal(oldProof)
	if _, err := tx.Exec("INSERT INTO control_target_registration_locations VALUES(?,?,?)", scope.operationID, previous, wire); err != nil {
		t.Fatal(err)
	}
	if _, _, err := readTargetMetadataTx(tx, original.ID); err == nil {
		t.Fatal("new evidence was grafted onto a legacy event")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	newScope, newMarker, newDocument := registrationFixture(t, recovered, "target-inspect-after-location-upgrade", original.ID, filepath.Join(t.TempDir(), "again"))
	if _, _, err := recovered.RegisterInspectedBackupTarget(newScope, newMarker, newDocument); !errors.Is(err, ErrTargetLocationRequired) {
		t.Fatal("a new relocation inherited the legacy exception", err)
	}
}

func TestTargetRegistrationLocationV22MigrationInterruptionsPreserveV21State(t *testing.T) {
	for _, phase := range []string{"CREATE TABLE control_store_meta_v22", "INSERT INTO control_store_meta_v22", "DROP TABLE control_store_meta", "ALTER TABLE control_store_meta_v22", "CREATE TABLE control_target_registration_locations", "CREATE TRIGGER control_target_registration_locations_no_replace", "INSERT INTO control_target_location_boundary", "INSERT INTO schema_migrations VALUES(22", "UPDATE control_store_meta SET schema_version=22", "PRAGMA user_version=22", "negative-clock"} {
		t.Run(phase, func(t *testing.T) {
			control := targetDeletePromotedStore(t, true)
			locationV21Fixture(t, control)
			scope, marker, document := registrationFixture(t, control, "target-inspect-before-v22", "target_legacy_location", filepath.Join(t.TempDir(), "original"))
			before, _, err := control.RegisterInspectedBackupTarget(scope, marker, document)
			if err != nil {
				t.Fatal(err)
			}
			fault := inventoryReadFault{query: phase, exec: true, armed: phase != "negative-clock"}
			installInventoryReadFault(t, control, &fault)
			clock := control.now
			if phase == "negative-clock" {
				control.now = func() int64 { return -1 }
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			failure := control.migrateToV22Tx(tx)
			if err := tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			control.schema = SchemaV21
			control.now = clock
			if failure == nil || (phase != "negative-clock" && !fault.injected) {
				t.Fatal("migration interruption was hidden", failure)
			}
			tx, err = control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if err := verifySchemaTx(tx, SchemaV21); err != nil {
				t.Fatal("rollback did not retain authentic v21 schema", err)
			}
			var count int
			if err := tx.QueryRow("SELECT COUNT(*) FROM sqlite_schema WHERE name='control_target_registration_locations'").Scan(&count); err != nil || count != 0 {
				t.Fatal("rollback retained half of the v22 layout", err)
			}
			if err := control.migrateToV22Tx(tx); err != nil {
				t.Fatal("same proven-rolled-back migration did not recover", err)
			}
			if err := tx.Commit(); err != nil {
				t.Fatal(err)
			}
			after, found, err := control.Get("target", before.ID)
			if err != nil || !found || !reflect.DeepEqual(before, after) {
				t.Fatal("migration retry changed original target", err)
			}
			if err := control.db.QueryRow("SELECT COUNT(*) FROM control_target_registration_locations").Scan(&count); err != nil || count != 0 {
				t.Fatal("migration retry invented witnesses", err)
			}
		})
	}
}

func TestTargetRegistrationLocationRetirementRejectsRetainedOrUnreadableCustody(t *testing.T) {
	control, record, _, _ := committedRelocationFixture(t)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := retireEmptyTargetLocationsTx(tx); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatal("retirement dropped retained location custody", err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if after, _, err := control.Get("target", record.ID); err != nil || !reflect.DeepEqual(after, record) {
		t.Fatal("refused retirement changed the original record", err)
	}
	for _, query := range []string{"", "SELECT COUNT(*) FROM control_target_registration_locations", "DROP TRIGGER control_target_registration_locations_no_replace"} {
		empty := targetDeletePromotedStore(t, true)
		fault := inventoryReadFault{query: query, exec: len(query) > 0 && query[:4] == "DROP", armed: query != ""}
		installInventoryReadFault(t, empty, &fault)
		tx, err := empty.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		err = retireEmptyTargetLocationsTx(tx)
		if query == "" {
			if err != nil {
				t.Fatal("proven empty location layout could not retire", err)
			}
		} else if err == nil || !fault.injected {
			t.Fatal("retirement ignored an actual read/DDL failure", err)
		}
		if err := tx.Rollback(); err != nil {
			t.Fatal(err)
		}
		tx, err = empty.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		if err := verifySchemaTx(tx, SchemaV22); err != nil {
			t.Fatal("rolled-back retirement changed original schema", err)
		}
		if err := tx.Rollback(); err != nil {
			t.Fatal(err)
		}
	}
}
