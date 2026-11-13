package store

import (
	"crypto/ed25519"
	"crypto/sha256"
	"errors"
	"math"
	"path/filepath"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

// Fixture signatures prove the Go verifier's checks, never a physical Rust read.
func targetObservationFixture(t *testing.T) (*actionv1.InspectFilesystemBackupTargetRequest, *actionv1.FilesystemBackupTargetInspectionReceipt, BackupTargetObservationContext) {
	t.Helper()
	request := &actionv1.InspectFilesystemBackupTargetRequest{SchemaVersion: 1, OperationId: "target-inspect-verifier",
		Fence: &commonv1.ActionFence{ActionId: "target-inspect-verifier", ExecutionEpoch: 3}, WriterFencingToken: 7,
		Path: filepath.Join(t.TempDir(), "original-target")}
	marker := []byte(`{"targetId":"target_original","targetNonce":"retained"}`)
	digest := sha256.Sum256(marker)
	receipt := &actionv1.FilesystemBackupTargetInspectionReceipt{SchemaVersion: 1, SignerPublicKey: promotionTestPublic,
		Observation: &actionv1.FilesystemBackupTargetObservation{SchemaVersion: 1, Fence: proto.Clone(request.Fence).(*commonv1.ActionFence),
			OperationId: request.OperationId, WriterFencingToken: request.WriterFencingToken, RequestedPath: request.Path,
			CanonicalPath: request.Path, MarkerPresent: true, MarkerJson: marker, MarkerSha256: digest[:],
			ObservedAtUnix: 1000, FleetId: "fleet-a", Environment: "production"}}
	signRegistrationFixture(t, receipt)
	return request, receipt, BackupTargetObservationContext{PublicKey: promotionTestPublic, FleetID: "fleet-a", Environment: "production", Now: 1000}
}

func targetLocationFixture(t *testing.T, request *actionv1.InspectFilesystemBackupTargetRequest) (*actionv1.ProbeFilesystemBackupTargetLocationRequest, *actionv1.FilesystemBackupTargetLocationReceipt) {
	t.Helper()
	location := &actionv1.ProbeFilesystemBackupTargetLocationRequest{SchemaVersion: request.SchemaVersion,
		Fence: proto.Clone(request.Fence).(*commonv1.ActionFence), OperationId: request.OperationId, Path: request.Path, WriterFencingToken: request.WriterFencingToken}
	receipt := &actionv1.FilesystemBackupTargetLocationReceipt{SchemaVersion: 1, SignerPublicKey: promotionTestPublic,
		Observation: &actionv1.FilesystemBackupTargetLocationObservation{SchemaVersion: 1, Fence: proto.Clone(request.Fence).(*commonv1.ActionFence),
			OperationId: request.OperationId, WriterFencingToken: request.WriterFencingToken, RequestedPath: request.Path,
			State: actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS, ObservedAtUnix: 1000, FleetId: "fleet-a", Environment: "production"}}
	signTargetLocationFixture(t, receipt, targetLocationSignatureDomain)
	return location, receipt
}

func signTargetLocationFixture(t *testing.T, receipt *actionv1.FilesystemBackupTargetLocationReceipt, domain string) {
	t.Helper()
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Observation)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(promotionTestPrivate, append([]byte(domain), wire...))
}

func TestTargetObservationRejectsInvalidReadRequests(t *testing.T) {
	request, _, _ := targetObservationFixture(t)
	if ValidateBackupTargetInspectionRequest(request) != nil {
		t.Fatal("valid marker request rejected")
	}
	for name, change := range map[string]func(*actionv1.InspectFilesystemBackupTargetRequest){
		"schema":         func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.SchemaVersion++ },
		"nil-fence":      func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.Fence = nil },
		"empty-action":   func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.Fence.ActionId = "" },
		"zero-epoch":     func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.Fence.ExecutionEpoch = 0 },
		"epoch-overflow": func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.Fence.ExecutionEpoch = math.MaxUint64 },
		"operation":      func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.OperationId += "-other" },
		"purpose": func(r *actionv1.InspectFilesystemBackupTargetRequest) {
			r.OperationId = "effect-write"
			r.Fence.ActionId = r.OperationId
		},
		"long-operation": func(r *actionv1.InspectFilesystemBackupTargetRequest) {
			r.OperationId += strings.Repeat("x", 129)
			r.Fence.ActionId = r.OperationId
		},
		"zero-writer":     func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.WriterFencingToken = 0 },
		"writer-overflow": func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.WriterFencingToken = math.MaxUint64 },
		"relative":        func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.Path = "relative-target" },
		"nul-path":        func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.Path += "\x00" },
		"long-path":       func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.Path += strings.Repeat("x", 32769) },
		"unknown":         func(r *actionv1.InspectFilesystemBackupTargetRequest) { r.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"unknown-fence": func(r *actionv1.InspectFilesystemBackupTargetRequest) {
			r.Fence.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
	} {
		t.Run(name, func(t *testing.T) {
			bad := proto.Clone(request).(*actionv1.InspectFilesystemBackupTargetRequest)
			change(bad)
			if !errors.Is(ValidateBackupTargetInspectionRequest(bad), ErrTargetRegistrationInvalid) {
				t.Fatal("invalid marker request accepted")
			}
			location := &actionv1.ProbeFilesystemBackupTargetLocationRequest{SchemaVersion: bad.SchemaVersion, Fence: bad.Fence,
				OperationId: bad.OperationId, WriterFencingToken: bad.WriterFencingToken, Path: bad.Path}
			location.ProtoReflect().SetUnknown(bad.ProtoReflect().GetUnknown())
			if !errors.Is(ValidateBackupTargetLocationRequest(location), ErrTargetRegistrationInvalid) {
				t.Fatal("invalid location request accepted")
			}
		})
	}
	if ValidateBackupTargetInspectionRequest(nil) == nil || ValidateBackupTargetLocationRequest(nil) == nil {
		t.Fatal("nil request accepted")
	}
}

func TestTargetObservationAcceptsSignedAbsenceWithoutAdmittingRegistration(t *testing.T) {
	request, receipt, context := targetObservationFixture(t)
	if err := VerifyBackupTargetInspectionReceipt(request, receipt, context); err != nil {
		t.Fatal(err)
	}
	receipt.Observation.MarkerPresent = false
	receipt.Observation.MarkerJson = nil
	receipt.Observation.MarkerSha256 = nil
	signRegistrationFixture(t, receipt)
	if err := VerifyBackupTargetInspectionReceipt(request, receipt, context); err != nil {
		t.Fatal("signed empty directory observation rejected", err)
	}
	control := targetDeletePromotedStore(t, true)
	scope, absent, document := registrationFixture(t, control, "target-inspect-absent-rejected", "target_absent", request.Path)
	absent.Observation.MarkerPresent = false
	absent.Observation.MarkerJson = nil
	absent.Observation.MarkerSha256 = nil
	signRegistrationFixture(t, absent)
	if _, _, err := control.RegisterInspectedBackupTarget(scope, absent, document); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("absence admitted a registration", err)
	}
	if _, exists, err := control.Get("target", "target_absent"); err != nil || exists {
		t.Fatal("rejected read created target state", err)
	}
	var witnesses int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_target_registrations WHERE operation_id=?", scope.operationID).Scan(&witnesses); err != nil || witnesses != 0 {
		t.Fatal("rejected read persisted registration evidence", err)
	}
}

func TestTargetObservationRejectsSignedRebindingAndMalformedEnvelopes(t *testing.T) {
	request, original, context := targetObservationFixture(t)
	for name, change := range map[string]func(*actionv1.FilesystemBackupTargetInspectionReceipt){
		"receipt-schema": func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.SchemaVersion++ },
		"schema":         func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.SchemaVersion++ },
		"epoch":          func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.Fence.ExecutionEpoch++ },
		"operation": func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.OperationId += "x"
			r.Observation.Fence.ActionId = r.Observation.OperationId
		},
		"writer":       func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.WriterFencingToken++ },
		"path":         func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.RequestedPath += "-other" },
		"canonical":    func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.CanonicalPath = "relative" },
		"digest":       func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.MarkerSha256[0] ^= 1 },
		"empty-marker": func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.MarkerJson = nil },
		"oversized-marker": func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.MarkerJson = make([]byte, 4*1024*1024+1)
			digest := sha256.Sum256(r.Observation.MarkerJson)
			r.Observation.MarkerSha256 = digest[:]
		},
		"absent-with-bytes": func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.MarkerPresent = false },
		"zero-time":         func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.ObservedAtUnix = 0 },
		"future":            func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.ObservedAtUnix++ },
		"stale":             func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.ObservedAtUnix -= 61 },
		"fleet":             func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.FleetId += "-other" },
		"environment":       func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.Environment += "-other" },
		"key":               func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.SignerPublicKey = "unpinned" },
		"unknown-receipt": func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"unknown-observation": func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"unknown-fence": func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.Fence.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
	} {
		t.Run(name, func(t *testing.T) {
			receipt := proto.Clone(original).(*actionv1.FilesystemBackupTargetInspectionReceipt)
			change(receipt)
			signRegistrationFixture(t, receipt)
			if err := VerifyBackupTargetInspectionReceipt(request, receipt, context); !errors.Is(err, ErrTargetRegistrationInvalid) {
				t.Fatal("signed incompatible marker read accepted", err)
			}
		})
	}
	bad := proto.Clone(original).(*actionv1.FilesystemBackupTargetInspectionReceipt)
	bad.Signature[0] ^= 1
	if VerifyBackupTargetInspectionReceipt(request, bad, context) == nil {
		t.Fatal("forged signature accepted")
	}
	for _, receipt := range []*actionv1.FilesystemBackupTargetInspectionReceipt{nil, {SchemaVersion: 1}, {SchemaVersion: 1, Observation: &actionv1.FilesystemBackupTargetObservation{}}} {
		if VerifyBackupTargetInspectionReceipt(request, receipt, context) == nil {
			t.Fatal("incomplete receipt accepted")
		}
	}
}

func TestTargetLocationRejectsUnresolvedRebindingAndPurposeConfusion(t *testing.T) {
	markerRequest, _, context := targetObservationFixture(t)
	request, original := targetLocationFixture(t, markerRequest)
	for _, state := range []actionv1.BackupTargetLocationState{actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING} {
		receipt := proto.Clone(original).(*actionv1.FilesystemBackupTargetLocationReceipt)
		receipt.Observation.State = state
		signTargetLocationFixture(t, receipt, targetLocationSignatureDomain)
		if err := VerifyBackupTargetLocationReceipt(request, receipt, context); err != nil {
			t.Fatal("signed resolved state rejected", err)
		}
	}
	for name, change := range map[string]func(*actionv1.FilesystemBackupTargetLocationReceipt){
		"schema":         func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.SchemaVersion++ },
		"receipt-schema": func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.SchemaVersion++ },
		"nil-fence":      func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.Fence = nil },
		"epoch":          func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.Fence.ExecutionEpoch++ },
		"operation":      func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.OperationId += "x" },
		"writer":         func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.WriterFencingToken++ },
		"path":           func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.RequestedPath += "-other" },
		"unresolved": func(r *actionv1.FilesystemBackupTargetLocationReceipt) {
			r.Observation.State = actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_UNSPECIFIED
		},
		"unknown-state":   func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.State = 99 },
		"fleet":           func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.FleetId += "-other" },
		"environment":     func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.Environment += "-other" },
		"zero-time":       func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.ObservedAtUnix = 0 },
		"future":          func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.ObservedAtUnix++ },
		"stale":           func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.ObservedAtUnix -= 61 },
		"key":             func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.SignerPublicKey = "unpinned" },
		"unknown-receipt": func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"unknown-observation": func(r *actionv1.FilesystemBackupTargetLocationReceipt) {
			r.Observation.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"unknown-fence": func(r *actionv1.FilesystemBackupTargetLocationReceipt) {
			r.Observation.Fence.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"oversized": func(r *actionv1.FilesystemBackupTargetLocationReceipt) {
			r.Observation.Environment = strings.Repeat("x", MaxBackupTargetLocationReceiptBytes)
		},
	} {
		t.Run(name, func(t *testing.T) {
			receipt := proto.Clone(original).(*actionv1.FilesystemBackupTargetLocationReceipt)
			change(receipt)
			signTargetLocationFixture(t, receipt, targetLocationSignatureDomain)
			if VerifyBackupTargetLocationReceipt(request, receipt, context) == nil {
				t.Fatal("signed incompatible location read accepted")
			}
		})
	}
	wrongPurpose := proto.Clone(original).(*actionv1.FilesystemBackupTargetLocationReceipt)
	signTargetLocationFixture(t, wrongPurpose, targetInspectionSignatureDomain)
	if VerifyBackupTargetLocationReceipt(request, wrongPurpose, context) == nil {
		t.Fatal("marker signature purpose accepted as existence evidence")
	}
	bad := proto.Clone(original).(*actionv1.FilesystemBackupTargetLocationReceipt)
	bad.Signature[0] ^= 1
	if VerifyBackupTargetLocationReceipt(request, bad, context) == nil {
		t.Fatal("forged location signature accepted")
	}
	for _, receipt := range []*actionv1.FilesystemBackupTargetLocationReceipt{nil, {SchemaVersion: 1}, {SchemaVersion: 1, Observation: &actionv1.FilesystemBackupTargetLocationObservation{}}} {
		if VerifyBackupTargetLocationReceipt(request, receipt, context) == nil {
			t.Fatal("incomplete location receipt accepted")
		}
	}
}

func TestTargetObservationRequiresPinnedDeploymentAndFreshTime(t *testing.T) {
	request, receipt, context := targetObservationFixture(t)
	location, locationReceipt := targetLocationFixture(t, request)
	for name, change := range map[string]func(*BackupTargetObservationContext){
		"zero-time":         func(c *BackupTargetObservationContext) { c.Now = 0 },
		"negative-time":     func(c *BackupTargetObservationContext) { c.Now = -1 },
		"empty-fleet":       func(c *BackupTargetObservationContext) { c.FleetID = "" },
		"empty-environment": func(c *BackupTargetObservationContext) { c.Environment = "" },
		"empty-key":         func(c *BackupTargetObservationContext) { c.PublicKey = "" },
		"stale":             func(c *BackupTargetObservationContext) { c.Now += 61 },
	} {
		t.Run(name, func(t *testing.T) {
			bad := context
			change(&bad)
			if VerifyBackupTargetInspectionReceipt(request, receipt, bad) == nil || VerifyBackupTargetLocationReceipt(location, locationReceipt, bad) == nil {
				t.Fatal("unbound or expired read accepted")
			}
		})
	}
	context.Now += 60
	if VerifyBackupTargetInspectionReceipt(request, receipt, context) != nil || VerifyBackupTargetLocationReceipt(location, locationReceipt, context) != nil {
		t.Fatal("inclusive freshness boundary rejected")
	}
}

func TestTargetObservationRejectsInvalidPinnedKeysAndUnencodableFields(t *testing.T) {
	request, marker, context := targetObservationFixture(t)
	location, existence := targetLocationFixture(t, request)
	badContext := context
	badContext.PublicKey = "not-base64"
	badMarker := proto.Clone(marker).(*actionv1.FilesystemBackupTargetInspectionReceipt)
	badMarker.SignerPublicKey = badContext.PublicKey
	badLocation := proto.Clone(existence).(*actionv1.FilesystemBackupTargetLocationReceipt)
	badLocation.SignerPublicKey = badContext.PublicKey
	if VerifyBackupTargetInspectionReceipt(request, badMarker, badContext) == nil || VerifyBackupTargetLocationReceipt(location, badLocation, badContext) == nil {
		t.Fatal("malformed pinned key accepted")
	}
	badContext = context
	badContext.Environment = "\xff"
	badMarker = proto.Clone(marker).(*actionv1.FilesystemBackupTargetInspectionReceipt)
	badMarker.Observation.Environment = badContext.Environment
	badLocation = proto.Clone(existence).(*actionv1.FilesystemBackupTargetLocationReceipt)
	badLocation.Observation.Environment = badContext.Environment
	if VerifyBackupTargetInspectionReceipt(request, badMarker, badContext) == nil || VerifyBackupTargetLocationReceipt(location, badLocation, badContext) == nil {
		t.Fatal("unencodable observation accepted")
	}
	if VerifyBackupTargetInspectionReceipt(nil, marker, context) == nil || VerifyBackupTargetLocationReceipt(nil, existence, context) == nil {
		t.Fatal("receipt accepted without original request")
	}
}
