package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	targetmodel "github.com/leizd/DeepSeek-Infra/go/internal/targets"
	"google.golang.org/protobuf/proto"
)

var s3StoreFixturePlacement = targetmodel.S3Placement{
	Endpoint: "https://fixture-provider.test", Region: "us-east-1", Bucket: "fixture-bucket", Prefix: "test/retained",
}

func s3RegistrationStoreFixture(t *testing.T, control *Control, suffix, id string, generation int64) (S3BackupTargetRegistrationScope, *actionv1.S3BackupTargetInspectionReceipt) {
	t.Helper()
	raw := []byte(`{"kind":"s3","bucket":"fixture-bucket","prefix":"test/retained","endpointUrl":"https://fixture-provider.test","region":"us-east-1","probe":false,"label":"` + suffix + `"}`)
	input, err := targetmodel.ReadInput(raw)
	if err != nil {
		t.Fatal(err)
	}
	metadata, err := input.ConfiguredS3Registration(s3StoreFixturePlacement)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(raw)
	scope, err := control.PrepareS3BackupTargetRegistration(PublicTargetOperationPrefix+suffix, "operator:test", hex.EncodeToString(digest[:]), metadata)
	if err != nil {
		t.Fatal(err)
	}
	request := scope.InspectionRequest()
	binding := control.BackupTargetObservationBinding()
	commit := strings.Repeat("0", 64)
	if generation != 0 {
		commit = fmt.Sprintf("%064x", generation)
	}
	receipt := &actionv1.S3BackupTargetInspectionReceipt{SchemaVersion: 1, SignerPublicKey: binding.PublicKey,
		Observation: &actionv1.S3BackupTargetObservation{SchemaVersion: 1, Fence: request.Fence, OperationId: request.OperationId,
			WriterFencingToken: request.WriterFencingToken, TargetIdentity: request.TargetIdentity, Bucket: s3StoreFixturePlacement.Bucket, Prefix: s3StoreFixturePlacement.Prefix,
			FleetId: binding.FleetID, Environment: binding.Environment, ObservedAtUnix: binding.Now,
			Identity: s3StoreFixtureObject(`{"schemaVersion":3,"targetId":"` + id + `","kind":"s3","bucket":"fixture-bucket","prefix":"test/retained","incarnationId":"inc_fixture","createdAt":"2026-11-19T00:00:00Z"}`),
			Head:     s3StoreFixtureObject(fmt.Sprintf(`{"schemaVersion":1,"incarnationId":"inc_fixture","targetGeneration":%d,"latestCommitHash":"%s"}`, generation, commit)),
		}}
	signS3StoreFixture(t, receipt)
	return scope, receipt
}

func s3StoreFixtureObject(raw string) *actionv1.S3BackupTargetObjectObservation {
	hash := sha256.Sum256([]byte(raw))
	return &actionv1.S3BackupTargetObjectObservation{Json: []byte(raw), Sha256: hash[:], Etag: `"fixture-etag"`, VersionId: "fixture-version"}
}

func signS3StoreFixture(t *testing.T, receipt *actionv1.S3BackupTargetInspectionReceipt) {
	t.Helper()
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Observation)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(promotionTestPrivate, append([]byte(targetS3SignatureDomain), wire...))
}

func TestTargetRegistrationS3KeepsOriginalVersionAndReplaysAfterTakeover(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, receipt := s3RegistrationStoreFixture(t, control, "s3-first", "target_s3", 2)
	first, err := control.RegisterObservedS3BackupTarget(scope, receipt)
	if err != nil || first.Revision != 1 || publicTargetGeneration(t, first) != 1 {
		t.Fatal(first, err)
	}
	var stored []byte
	if err := control.db.QueryRow("SELECT observation FROM control_target_registrations WHERE operation_id=?", scope.operation).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	decoded := &actionv1.S3BackupTargetInspectionReceipt{}
	if err := proto.Unmarshal(stored, decoded); err != nil || !proto.Equal(decoded, receipt) ||
		!bytes.Equal(decoded.Observation.Head.Json, receipt.Observation.Head.Json) {
		t.Fatal("exact provider checkpoint was not retained", err)
	}
	for index, failure := range []struct {
		id   string
		gen  int64
		want error
	}{
		{"target_s3", 1, ErrS3BackupTargetHeadConflict},
		{"target_unrelated", 4, ErrTargetCloneDetected},
	} {
		scope, proof := s3RegistrationStoreFixture(t, control, fmt.Sprintf("conflict-%d", index), failure.id, failure.gen)
		if _, err := control.RegisterObservedS3BackupTarget(scope, proof); !errors.Is(err, failure.want) {
			t.Fatal("registry conflict not refused", err)
		}
	}
	secondScope, newer := s3RegistrationStoreFixture(t, control, "s3-second", "target_s3", 3)
	staleScope, stale := s3RegistrationStoreFixture(t, control, "s3-stale", "target_s3", 4)
	second, err := control.RegisterObservedS3BackupTarget(secondScope, newer)
	if err != nil || second.Revision != 2 || publicTargetGeneration(t, second) != 2 {
		t.Fatal(second, err)
	}
	if _, err := control.RegisterObservedS3BackupTarget(staleScope, stale); !errors.Is(err, ErrRevisionConflict) {
		t.Fatal("a stale registry snapshot reached publication", err)
	}
	if _, err := control.DeleteBackupTarget("s3-delete", "s3-delete", "operator:test", first.ID); err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	if replay, result, found, err := reopened.ReplayPublicBackupTargetRegistration(scope.operation, scope.actor, scope.frame.BodySHA256); err != nil || !found ||
		!reflect.DeepEqual(first, replay) || result.Status != OperatorMutationAlreadyApplied {
		t.Fatal("lost reply did not recover the original result", replay, result, found, err)
	}
	if err := reopened.Rollback(0); !errors.Is(err, ErrSchemaInactive) {
		t.Fatal("rollback retired retained S3 custody", err)
	}
}

func TestTargetRegistrationS3RejectsUnboundPartialAndInvalidProviderLayout(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, good := s3RegistrationStoreFixture(t, control, "s3-unbound", "target_s3", 0)
	for _, row := range []struct {
		name   string
		mutate func(*actionv1.S3BackupTargetObservation)
		want   error
	}{
		{"absent-head", func(o *actionv1.S3BackupTargetObservation) { o.Head = nil }, ErrS3BackupTargetLayoutIncomplete},
		{"absent-identity", func(o *actionv1.S3BackupTargetObservation) { o.Identity = nil }, ErrS3BackupTargetLayoutIncomplete},
		{"wrong-fingerprint", func(o *actionv1.S3BackupTargetObservation) { o.TargetIdentity[0] ^= 1 }, ErrTargetRegistrationInvalid},
		{"wrong-bucket", func(o *actionv1.S3BackupTargetObservation) { o.Bucket = "foreign-bucket" }, ErrTargetRegistrationInvalid},
		{"wrong-prefix", func(o *actionv1.S3BackupTargetObservation) { o.Prefix = "wrong-prefix" }, ErrTargetRegistrationInvalid},
		{"foreign-incarnation", func(o *actionv1.S3BackupTargetObservation) {
			o.Head = s3StoreFixtureObject(strings.ReplaceAll(string(o.Head.Json), "inc_fixture", "inc_foreign"))
		}, ErrTargetRegistrationInvalid},
		{"wrong-field-case", func(o *actionv1.S3BackupTargetObservation) {
			o.Identity = s3StoreFixtureObject(strings.ReplaceAll(string(o.Identity.Json), "targetId", "TargetId"))
		}, ErrTargetRegistrationInvalid},
		{"bad-timestamp", func(o *actionv1.S3BackupTargetObservation) {
			o.Identity = s3StoreFixtureObject(strings.ReplaceAll(string(o.Identity.Json), "2026-11-19T00:00:00Z", "bad"))
		}, ErrTargetRegistrationInvalid},
	} {
		t.Run(row.name, func(t *testing.T) {
			proof := proto.Clone(good).(*actionv1.S3BackupTargetInspectionReceipt)
			row.mutate(proof.Observation)
			signS3StoreFixture(t, proof)
			if _, err := control.RegisterObservedS3BackupTarget(scope, proof); !errors.Is(err, row.want) {
				t.Fatalf("layout accepted: %v", err)
			}
		})
	}
	if size, err := control.OperatorJournalSize(); err != nil || size != 0 {
		t.Fatal(size, err)
	}
}

func TestTargetRegistrationS3ContextCannotBeChangedOrLoseItsSignedBinding(t *testing.T) {
	for _, mode := range []string{"update", "delete"} {
		t.Run(mode, func(t *testing.T) {
			control := targetDeletePromotedStore(t, true)
			scope, proof := s3RegistrationStoreFixture(t, control, "s3-bound-context", "target_s3", 0)
			if _, err := control.RegisterObservedS3BackupTarget(scope, proof); err != nil {
				t.Fatal(err)
			}
			frame := scope.frame
			query := "DELETE FROM control_target_registration_s3 WHERE operation_id=?"
			args := []any{scope.operation}
			if mode == "update" {
				frame.BodySHA256 = strings.Repeat("f", 64)
				raw, err := json.Marshal(frame)
				if err != nil {
					t.Fatal(err)
				}
				query = "UPDATE control_target_registration_s3 SET context_json=? WHERE operation_id=?"
				args = []any{string(raw), scope.operation}
			}
			if _, err := control.db.Exec(query, args...); err == nil {
				t.Fatal("S3 custody became mutable")
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			trigger := "control_target_registration_s3_no_" + mode
			var schema string
			if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", trigger).Scan(&schema); err != nil {
				t.Fatal(err)
			}
			if _, err := tx.Exec("DROP TRIGGER " + trigger); err != nil {
				t.Fatal(err)
			}
			if _, err := tx.Exec(query, args...); err != nil {
				t.Fatal(err)
			}
			if _, err := tx.Exec(schema); err != nil {
				t.Fatal(err)
			}
			if err := tx.Commit(); err != nil {
				t.Fatal(err)
			}
			// The schema is restored so this reaches retained witness and
			// request-digest validation with corrupt historical content.
			if _, _, _, err := control.ReplayPublicBackupTargetRegistration(scope.operation, scope.actor, frame.BodySHA256); !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("altered or missing context passed history validation", err)
			}
		})
	}
}

func TestTargetRegistrationS3WitnessFailureRollsBackRegistryAndOperation(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, proof := s3RegistrationStoreFixture(t, control, "s3-transaction", "target_s3", 0)
	if _, err := control.db.Exec(`CREATE TRIGGER reject_s3_registration_context BEFORE INSERT ON control_target_registration_s3
		BEGIN SELECT RAISE(ABORT,'S3_CONTEXT_TEST_WRITE_FAILURE'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RegisterObservedS3BackupTarget(scope, proof); err == nil || !strings.Contains(err.Error(), "S3_CONTEXT_TEST_WRITE_FAILURE") {
		t.Fatal("S3 witness insertion did not fail", err)
	}
	if _, err := control.db.Exec("DROP TRIGGER reject_s3_registration_context"); err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"targets", "control_target_registrations", "control_target_registration_s3", "control_target_registration_identity", "control_operator_mutations"} {
		var count int
		if err := control.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("%s remained after rolled-back mutation: %d %v", table, count, err)
		}
	}
	if record, err := control.RegisterObservedS3BackupTarget(scope, proof); err != nil || record.Revision != 1 {
		t.Fatal("retry of an unapplied transaction failed", record, err)
	}
}
