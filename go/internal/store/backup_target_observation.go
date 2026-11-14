package store

import (
	"crypto/ed25519"
	"math"
	"path/filepath"
	"strings"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

const MaxBackupTargetInspectionReceiptBytes = maximumTargetInspectionBytes
const MaxBackupTargetLocationReceiptBytes = 128 * 1024
const targetLocationSignatureDomain = "deepseek-infra:backup-target-location-v1\x00"

// Only configured public metadata enters Go. The private custody key remains
// in Rust. A verified read does not admit a mutation or install an epoch.
type BackupTargetObservationContext struct {
	PublicKey, FleetID, Environment string
	Now                             int64
}

func validBackupTargetReadRequest(version uint32, fence *commonv1.ActionFence, operation, path string, writer uint64, messages ...proto.Message) bool {
	return version == 1 && internalprotocol.ValidateFence(fence) == nil && fence.ExecutionEpoch <= math.MaxInt64 &&
		operation == fence.ActionId && strings.HasPrefix(operation, targetInspectionOperationPrefix) && len(operation) <= 128 &&
		writer > 0 && writer <= math.MaxInt64 && filepath.IsAbs(path) && len(path) <= 32768 && !strings.ContainsRune(path, 0) && nativeKnown(messages...)
}

func ValidateBackupTargetInspectionRequest(request *actionv1.InspectFilesystemBackupTargetRequest) error {
	if request == nil || !validBackupTargetReadRequest(request.SchemaVersion, request.Fence, request.OperationId, request.Path,
		request.WriterFencingToken, request, request.Fence) {
		return ErrTargetRegistrationInvalid
	}
	return nil
}

func ValidateBackupTargetLocationRequest(request *actionv1.ProbeFilesystemBackupTargetLocationRequest) error {
	if request == nil || !validBackupTargetReadRequest(request.SchemaVersion, request.Fence, request.OperationId, request.Path,
		request.WriterFencingToken, request, request.Fence) {
		return ErrTargetRegistrationInvalid
	}
	return nil
}

func VerifyBackupTargetInspectionReceipt(request *actionv1.InspectFilesystemBackupTargetRequest, receipt *actionv1.FilesystemBackupTargetInspectionReceipt, context BackupTargetObservationContext) error {
	if ValidateBackupTargetInspectionRequest(request) != nil || context.Now <= 0 || context.FleetID == "" || context.Environment == "" ||
		verifyTargetObservationEnvelope(receipt, context.PublicKey, context.FleetID, context.Environment) != nil {
		return ErrTargetRegistrationInvalid
	}
	o := receipt.Observation
	if !proto.Equal(request.Fence, o.Fence) || request.OperationId != o.OperationId || request.WriterFencingToken != o.WriterFencingToken ||
		request.Path != o.RequestedPath || request.RequireFullContainment != o.FullContainmentChecked || o.ObservedAtUnix > context.Now || context.Now-o.ObservedAtUnix > 60 {
		return ErrTargetRegistrationInvalid
	}
	return nil
}

func VerifyBackupTargetLocationReceipt(request *actionv1.ProbeFilesystemBackupTargetLocationRequest, receipt *actionv1.FilesystemBackupTargetLocationReceipt, context BackupTargetObservationContext) error {
	if ValidateBackupTargetLocationRequest(request) != nil || context.Now <= 0 || context.FleetID == "" || context.Environment == "" ||
		receipt == nil || receipt.Observation == nil || receipt.SchemaVersion != 1 || receipt.SignerPublicKey != context.PublicKey ||
		len(receipt.Signature) != ed25519.SignatureSize || proto.Size(receipt) > MaxBackupTargetLocationReceiptBytes {
		return ErrTargetRegistrationInvalid
	}
	o := receipt.Observation
	if o.SchemaVersion != 1 || o.Fence == nil || !nativeKnown(receipt, o, o.Fence) || !proto.Equal(request.Fence, o.Fence) ||
		o.OperationId != request.OperationId || o.WriterFencingToken != request.WriterFencingToken || o.RequestedPath != request.Path ||
		o.FleetId != context.FleetID || o.Environment != context.Environment || o.ObservedAtUnix <= 0 ||
		o.ObservedAtUnix > context.Now || context.Now-o.ObservedAtUnix > 60 {
		return ErrTargetRegistrationInvalid
	}
	if request.ResolvePath {
		if o.State != actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_UNSPECIFIED || !filepath.IsAbs(o.CanonicalPath) || len(o.CanonicalPath) > 32768 || strings.ContainsRune(o.CanonicalPath, 0) {
			return ErrTargetRegistrationInvalid
		}
	} else if o.CanonicalPath != "" || (o.State != actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS && o.State != actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING) {
		return ErrTargetRegistrationInvalid
	}
	key, err := decodeFixedBase64(context.PublicKey, ed25519.PublicKeySize)
	if err != nil {
		return ErrTargetRegistrationInvalid
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(o)
	if err != nil || !ed25519.Verify(key, append([]byte(targetLocationSignatureDomain), wire...), receipt.Signature) {
		return ErrTargetRegistrationInvalid
	}
	return nil
}
