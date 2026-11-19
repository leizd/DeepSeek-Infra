package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func TestS3BackupTargetReceiptBindsPresentAndAbsentObjectsToOriginalScope(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	scope := BackupTargetObservationContext{PublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "fleet-a",
		Environment: "test", Now: 1000}
	op := "target-inspect-s3-layout"
	target := sha256.Sum256([]byte("configured fixture"))
	req := &actionv1.InspectS3BackupTargetRequest{SchemaVersion: 1, Fence: &commonv1.ActionFence{ActionId: op, ExecutionEpoch: 3},
		OperationId: op, WriterFencingToken: 11, TargetIdentity: target[:]}
	o := &actionv1.S3BackupTargetObservation{SchemaVersion: 1, Fence: req.Fence, OperationId: op, WriterFencingToken: 11,
		TargetIdentity: target[:], Bucket: "configured-fixture", ObservedAtUnix: 995, FleetId: scope.FleetID, Environment: scope.Environment}
	sign := func(read *actionv1.S3BackupTargetObservation) *actionv1.S3BackupTargetInspectionReceipt {
		wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(read)
		if err != nil {
			t.Fatal(err)
		}
		return &actionv1.S3BackupTargetInspectionReceipt{SchemaVersion: 1, Observation: read, SignerPublicKey: scope.PublicKey,
			Signature: ed25519.Sign(private, append([]byte(targetS3SignatureDomain), wire...))}
	}
	absent := sign(o)
	if err := VerifyBackupTargetS3Receipt(req, absent, scope); err != nil {
		t.Fatalf("authentic absence observations: %v", err)
	}
	wrongKey := scope
	wrongKey.PublicKey = "bad-public-key"
	changed := proto.Clone(absent).(*actionv1.S3BackupTargetInspectionReceipt)
	changed.SignerPublicKey = wrongKey.PublicKey
	if err := VerifyBackupTargetS3Receipt(req, changed, wrongKey); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatalf("invalid signer was accepted: %v", err)
	}
	if err := VerifyBackupTargetS3Receipt(nil, absent, scope); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal(err)
	}
	withObject := func(body []byte) *actionv1.S3BackupTargetObservation {
		result := proto.Clone(o).(*actionv1.S3BackupTargetObservation)
		digest := sha256.Sum256(body)
		result.Identity = &actionv1.S3BackupTargetObjectObservation{Json: body, Sha256: digest[:], Etag: "\"opaque\"", VersionId: "v123"}
		return result
	}
	positive := withObject([]byte("{\"targetId\":\"target_a\"}\n"))
	if err := VerifyBackupTargetS3Receipt(req, sign(positive), scope); err != nil {
		t.Fatalf("authentic object failed verification: %v", err)
	}
	for name, read := range map[string]*actionv1.S3BackupTargetObservation{
		"invalid-json": withObject([]byte("{malformed")),
		"non-object":   withObject([]byte("[]")),
		"large-json":   withObject(bytes.Repeat([]byte(" "), maxBackupTargetS3ObjectBytes+1)),
	} {
		t.Run(name, func(t *testing.T) {
			if err := VerifyBackupTargetS3Receipt(req, sign(read), scope); !errors.Is(err, ErrTargetRegistrationInvalid) {
				t.Fatal(err)
			}
		})
	}
}
