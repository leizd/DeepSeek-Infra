package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

// Fixture signatures exercise Go's retained control boundary, never physical
// Rust observations or permission to publish production promotion evidence.
func TestTargetRegistrationMalformedRequestsCannotEnterTheJournal(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "target")
	for _, input := range [][4]string{
		{"ordinary-operation", "ordinary-operation", "operator:test", path},
		{"target-inspect-one", "different-action", "operator:test", path},
		{"target-inspect-relative", "target-inspect-relative", "operator:test", "relative"},
		{"target-inspect-actor", "target-inspect-actor", "", path},
		{strings.Repeat("target-inspect-long", 10), strings.Repeat("target-inspect-long", 10), "operator:test", path},
	} {
		if _, err := control.PrepareBackupTargetRegistration(input[0], input[1], input[2], input[3]); err == nil {
			t.Fatalf("invalid preparation created an observation scope: %q", input)
		}
	}
	scope, original, document := registrationFixture(t, control, "target-inspect-malformed", "target_original", path)
	changes := []struct {
		name string
		edit func(*actionv1.FilesystemBackupTargetInspectionReceipt)
	}{
		{"receipt-version", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.SchemaVersion = 2 }},
		{"missing-observation", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation = nil }},
		{"short-signature", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Signature = r.Signature[:63] }},
		{"unknown-receipt-field", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 1})
		}},
		{"unknown-observation-field", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 1})
			signRegistrationFixture(t, r)
		}},
		{"unknown-fence-field", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.Fence.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 1})
			signRegistrationFixture(t, r)
		}},
		{"invalid-canonical-path", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.CanonicalPath = "relative"
			signRegistrationFixture(t, r)
		}},
		{"wrong-requested-path", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.RequestedPath += "-other"
			signRegistrationFixture(t, r)
		}},
		{"wrong-operation-scope", func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.OperationId += "-other"
			r.Observation.Fence.ActionId = r.Observation.OperationId
			signRegistrationFixture(t, r)
		}},
	}
	for _, change := range changes {
		t.Run(change.name, func(t *testing.T) {
			receipt := proto.Clone(original).(*actionv1.FilesystemBackupTargetInspectionReceipt)
			change.edit(receipt)
			if _, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); err == nil {
				t.Fatal("malformed or unbound signed observation reached the registry")
			}
		})
	}
	if _, _, err := control.RegisterInspectedBackupTarget(scope, nil, document); err == nil {
		t.Fatal("missing observation reached the registry")
	}
	for _, body := range []json.RawMessage{nil, json.RawMessage(`null`), json.RawMessage(`[]`), json.RawMessage(`{`), append(append(json.RawMessage(nil), document...), []byte(` {}`)...)} {
		if _, _, err := control.RegisterInspectedBackupTarget(scope, original, body); err == nil {
			t.Fatal("a malformed or multiple JSON document reached the registry")
		}
	}
	for _, change := range []func(*BackupTargetRegistrationScope){
		func(s *BackupTargetRegistrationScope) { s.actor = "" },
		func(s *BackupTargetRegistrationScope) { s.requestedPath += "-other" },
		func(s *BackupTargetRegistrationScope) { s.writerFence++ },
		func(s *BackupTargetRegistrationScope) { s.cutoverRevision++ },
		func(s *BackupTargetRegistrationScope) { s.snapshot = "different-catalog" },
	} {
		bad := scope
		change(&bad)
		if _, _, err := control.RegisterInspectedBackupTarget(bad, original, document); err == nil {
			t.Fatal("a substituted authority/catalog scope reached the registry")
		}
	}
	for _, raw := range []string{`null`, `{`, `{"topologyGeneration":"1"}`, `{"topologyGeneration":0}`, `{"topologyGeneration":1.5}`, `{"topologyGeneration":9223372036854775808}`} {
		if _, err := targetPublicGeneration(Record{Payload: json.RawMessage(raw)}); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt persisted public generation was accepted: %s %v", raw, err)
		}
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
		t.Fatalf("invalid observations changed the original journal: %d %v", count, err)
	}
	// A retained marker without targetNonce uses the original empty string. Its
	// successful path must remain legal after all failed attempts above.
	receipt := proto.Clone(original).(*actionv1.FilesystemBackupTargetInspectionReceipt)
	receipt.Observation.MarkerJson = []byte(`{"targetId":"target_original"}`)
	digest := sha256.Sum256(receipt.Observation.MarkerJson)
	receipt.Observation.MarkerSha256 = digest[:]
	signRegistrationFixture(t, receipt)
	body := bytes.Replace(document, []byte(`"targetNonce":"original-nonce"`), []byte(`"targetNonce":""`), 1)
	if record, _, err := control.RegisterInspectedBackupTarget(scope, receipt, body); err != nil || record.Revision != 1 {
		t.Fatalf("valid retained empty-nonce registration failed after refusals: %+v %v", record, err)
	}
}

func TestTargetRegistrationQueryFailuresPreserveOriginalRowsAndReplay(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "target")
	scope, receipt, document := registrationFixture(t, control, "target-inspect-query-original", "target_original", path)
	current, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
	if err != nil {
		t.Fatal(err)
	}
	faults := []inventoryReadFault{
		{query: "PRAGMA user_version"},
		{query: "SELECT signer_public_key,fleet_id,environment FROM control_target_registration_identity"},
		{query: "SELECT COUNT(*) FROM control_target_registrations"},
		{query: "FROM targets ORDER BY id"},
		{query: "FROM targets ORDER BY id", cursor: true},
		{query: "FROM targets ORDER BY id", malformed: true},
		{query: "SELECT DISTINCT record_id FROM control_events WHERE domain='target'"},
		{query: "SELECT DISTINCT record_id FROM control_events WHERE domain='target'", cursor: true},
		{query: "SELECT operation_id,revision,public_generation,observation FROM control_target_registrations"},
		{query: "SELECT operation_id,revision,public_generation,observation FROM control_target_registrations", cursor: true},
		{query: "SELECT operation_id,revision,public_generation,observation FROM control_target_registrations", malformed: true},
		{query: "SELECT COUNT(*) FROM control_operator_mutations o JOIN control_events e"},
		{query: "FROM targets WHERE id = ?"},
	}
	for index, value := range faults {
		t.Run(fmt.Sprintf("read-%d", index), func(t *testing.T) {
			op := fmt.Sprintf("target-inspect-query-%d", index)
			scope, receipt, document := registrationFixture(t, control, op, "target_original", path)
			before, _ := control.OperatorJournalSize()
			fault := value
			installInventoryReadFault(t, control, &fault)
			fault.armed = true
			if _, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); err == nil || !fault.injected {
				t.Fatalf("actual SQLite read failure did not stop publication: %v injected=%v", err, fault.injected)
			}
			after, exists, err := control.Get("target", current.ID)
			count, countErr := control.OperatorJournalSize()
			if err != nil || !exists || !reflect.DeepEqual(after, current) || countErr != nil || count != before {
				t.Fatalf("read failure changed original row or journal: %+v %v count=%d before=%d %v", after, err, count, before, countErr)
			}
			recovered, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
			if err != nil || recovered.Revision != current.Revision+1 || publicTargetGeneration(t, recovered) != publicTargetGeneration(t, current)+1 {
				t.Fatalf("proven rolled-back request failed to reconcile exactly: %+v %v", recovered, err)
			}
			current = recovered
		})
	}
	for index, query := range []string{"FROM targets ORDER BY id", "PRAGMA user_version", "SELECT COUNT(*) FROM control_target_registrations"} {
		t.Run(fmt.Sprintf("prepare-%d", index), func(t *testing.T) {
			fault := inventoryReadFault{query: query}
			installInventoryReadFault(t, control, &fault)
			fault.armed = true
			op := fmt.Sprintf("target-inspect-prepare-%d", index)
			if _, err := control.PrepareBackupTargetRegistration(op, op, "operator:test", path); err == nil || !fault.injected {
				t.Fatalf("unreadable catalog produced an observation scope: %v injected=%v", err, fault.injected)
			}
			after, exists, err := control.Get("target", current.ID)
			if err != nil || !exists || !reflect.DeepEqual(after, current) {
				t.Fatal("failed preparation changed the target")
			}
		})
	}
}

func TestTargetRegistrationClosedTransactionsCannotMasqueradeAsMissingState(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "target")
	scope, receipt, document := registrationFixture(t, control, "target-inspect-closed-tx", "target_original", path)
	record, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	checks := []func() error{
		func() error { _, err := targetRegistryDigestTx(tx); return err },
		func() error { _, err := loadTargetRegistrationBindingsTx(tx, record.ID); return err },
		func() error { _, err := targetRegistrationSchemaActiveTx(tx); return err },
		func() error { return targetRegistrationOperationAllowedTx(tx, "target-inspect-uncommitted", false) },
		func() error { _, _, err := targetBoundGenerationTx(tx, record); return err },
		func() error { _, err := nextTargetPublicGenerationTx(tx, record); return err },
		func() error { return verifyTargetRegistrationSchemaTx(tx) },
		func() error { return control.verifyTargetRegistrationIdentityTx(tx) },
		func() error { return retireEmptyTargetRegistrationsTx(tx) },
		func() error { return control.migrateToV21Tx(tx) },
	}
	for index, check := range checks {
		if err := check(); err == nil {
			t.Fatalf("closed transaction returned healthy/missing state at boundary %d", index)
		}
	}
	after, exists, err := control.Get("target", record.ID)
	if err != nil || !exists || !reflect.DeepEqual(after, record) {
		t.Fatal("closed transaction altered the durable original target")
	}
}

func TestTargetRegistrationHistoryRejectsWrongGenerationsFencesAndWitnessBytes(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "target")
	var before, record Record
	for _, op := range []string{"target-inspect-history-first", "target-inspect-history-second"} {
		scope, receipt, document := registrationFixture(t, control, op, "target_original", path)
		before = record
		var err error
		record, _, err = control.RegisterInspectedBackupTarget(scope, receipt, document)
		if err != nil {
			t.Fatal(err)
		}
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	bindings, err := loadTargetRegistrationBindingsTx(tx, record.ID)
	if err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	_ = tx.Rollback()
	binding := bindings[record.Revision]
	metadata := storedRecordMetadata{writerToken: control.token, timestamp: control.now()}
	if err := validateTargetRegistrationEvent(binding, before, record, metadata); err != nil {
		t.Fatal(err)
	}
	for _, mutate := range []func(*targetRegistrationBinding, *Record, *Record, *storedRecordMetadata){
		func(_ *targetRegistrationBinding, _ *Record, r *Record, _ *storedRecordMetadata) {
			r.State = TombstoneState
		},
		func(_ *targetRegistrationBinding, _ *Record, r *Record, _ *storedRecordMetadata) { r.Revision++ },
		func(_ *targetRegistrationBinding, _ *Record, _ *Record, m *storedRecordMetadata) { m.writerToken++ },
		func(_ *targetRegistrationBinding, _ *Record, _ *Record, m *storedRecordMetadata) { m.timestamp-- },
		func(_ *targetRegistrationBinding, _ *Record, _ *Record, m *storedRecordMetadata) { m.timestamp += 61 },
		func(b *targetRegistrationBinding, _ *Record, _ *Record, _ *storedRecordMetadata) { b.generation++ },
		func(b *targetRegistrationBinding, _ *Record, _ *Record, _ *storedRecordMetadata) {
			b.result.PayloadDigest = "wrong-original-payload"
		},
		func(_ *targetRegistrationBinding, p *Record, _ *Record, _ *storedRecordMetadata) {
			p.Payload = json.RawMessage(`{}`)
		},
		func(_ *targetRegistrationBinding, p *Record, _ *Record, _ *storedRecordMetadata) {
			p.Payload = json.RawMessage(`{"topologyGeneration":9223372036854775807}`)
		},
		func(_ *targetRegistrationBinding, _ *Record, r *Record, _ *storedRecordMetadata) {
			r.Payload = bytes.Replace(r.Payload, []byte(`"targetNonce":"original-nonce"`), []byte(`"targetNonce":"other"`), 1)
		},
	} {
		b, p, r, m := binding, before, record, metadata
		mutate(&b, &p, &r, &m)
		if !errors.Is(validateTargetRegistrationEvent(b, p, r, m), ErrCorruptRecord) {
			t.Fatal("corrupt retained history was accepted")
		}
	}
	var original []byte
	if err := control.db.QueryRow("SELECT observation FROM control_target_registrations WHERE operation_id=?", binding.operation).Scan(&original); err != nil {
		t.Fatal(err)
	}
	for _, encoded := range [][]byte{[]byte{0xff}, append(append([]byte(nil), original...), []byte{0xa0, 0x06, 1}...)} {
		replaceRegistrationWitness(t, control, binding.operation, encoded)
		if _, _, err := control.Get("target", record.ID); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt physical witness looked healthy: %v", err)
		}
		replaceRegistrationWitness(t, control, binding.operation, original)
		if after, exists, err := control.Get("target", record.ID); err != nil || !exists || !reflect.DeepEqual(after, record) {
			t.Fatal("restoring original witness bytes did not restore the original record")
		}
	}
	// Public/internal generation arithmetic rejects overflow or a mismatched
	// retained counter rather than creating an apparently current revision.
	tx, err = control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	bad := record
	bad.Payload = json.RawMessage(`{"topologyGeneration":1}`)
	if _, err := nextTargetPublicGenerationTx(tx, bad); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("a mismatched independent generation was incremented")
	}
	if _, err := nextTargetPublicGenerationTx(tx, Record{Domain: "target", ID: "unbound", Revision: math.MaxInt64}); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("an exhausted legacy revision was incremented")
	}
}

func replaceRegistrationWitness(t *testing.T, control *Control, operation string, encoded []byte) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("DROP TRIGGER control_target_registrations_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("UPDATE control_target_registrations SET observation=? WHERE operation_id=?", encoded, operation); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(targetRegistrationSchemaObjects["control_target_registrations_no_update"]); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
}
