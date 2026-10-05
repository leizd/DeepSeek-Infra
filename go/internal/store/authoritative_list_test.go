package store

import (
	"errors"
	"testing"
)

func TestAuthoritativeListRequiresPromotionAndValidatesHistory(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, err := control.ListAuthoritativeRecords("unknown"); !errors.Is(err, ErrUnknownDomain) {
		t.Fatalf("unknown domain: %v", err)
	}
	if _, err := control.ListAuthoritativeRecords("policy"); !errors.Is(err, ErrDomainNotAuthoritative) {
		t.Fatalf("shadow list must fail closed: %v", err)
	}
	checkpoint, dual, imported := importEmptyPythonSource(t, control, "policy")
	cutover, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported))
	if err != nil {
		t.Fatal(err)
	}
	records, err := control.ListAuthoritativeRecords("policy")
	if err != nil || len(records) != 0 {
		t.Fatalf("attested empty list: %+v %v", records, err)
	}
	_, public := rfc8032MutationKeys(t)
	for index, id := range []string{"z-policy", "a-policy"} {
		operationID := repeatHex("a")
		if index == 1 {
			operationID = repeatHex("b")
		}
		raw := signedApply(t, cutover, public, operationID, id, "ACTIVE", 1,
			map[string]any{"policyId": id, "schedule": map[string]any{"cron": "0 3 * * *", "timezone": "UTC"}})
		if result, err := control.ApplyMutation(raw, applyAuthority(public)); err != nil || result.Status != MutationApplied {
			t.Fatalf("write %s: %+v %v", id, result, err)
		}
	}
	records, err = control.ListAuthoritativeRecords("policy")
	if err != nil || len(records) != 2 || records[0].ID != "a-policy" || records[1].ID != "z-policy" {
		t.Fatalf("ordered authoritative list: %+v %v", records, err)
	}
	if _, err := control.db.Exec("DELETE FROM policies WHERE id = ?", "a-policy"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ListAuthoritativeRecords("policy"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("orphaned history must fail closed: %v", err)
	}
}

func TestAuthoritativeListClosedStoreIsDenied(t *testing.T) {
	control := openAuthority(t)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ListAuthoritativeRecords("policy"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed list: %v", err)
	}
}

func TestAuthoritativeListRejectsCorruptCurrentRecord(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, dual, imported := importEmptyPythonSource(t, control, "policy")
	cutover, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported))
	if err != nil {
		t.Fatal(err)
	}
	_, public := rfc8032MutationKeys(t)
	raw := signedApply(t, cutover, public, repeatHex("d"), "corrupt-policy", "ACTIVE", 1,
		map[string]any{"policyId": "corrupt-policy"})
	if result, err := control.ApplyMutation(raw, applyAuthority(public)); err != nil || result.Status != MutationApplied {
		t.Fatalf("write before fault: %+v %v", result, err)
	}
	if _, err := control.db.Exec("UPDATE policies SET payload_json = '{}' WHERE id = ?", "corrupt-policy"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ListAuthoritativeRecords("policy"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt payload must fail closed: %v", err)
	}
}

func TestAuthoritativeListRefusesStorageFailure(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if err := control.db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ListAuthoritativeRecords("policy"); err == nil {
		t.Fatal("closed database was read as an empty policy list")
	}
}

func TestAuthoritativeListRefusesSchemaDrift(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, err := control.db.Exec("DROP TABLE control_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ListAuthoritativeRecords("policy"); err == nil {
		t.Fatal("missing event history table was read as an empty policy list")
	}
}

func TestAuthoritativeListRefusesInactiveSchema(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	control.schema = SchemaV1
	if _, err := control.ListAuthoritativeRecords("policy"); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("inactive store schema must fail closed: %v", err)
	}
}
