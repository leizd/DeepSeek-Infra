package action

import (
	"crypto/sha256"
	"encoding/hex"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// storageDispatchIntent projects the existing qualification RPC to closed control
// metadata. Rust remains responsible for payload verification and signed authority.
// This does not enable production RPCs or make Go a production byte mover.
func storageDispatchIntent(req *actionv1.StorageMutationRequest, record store.Record) (store.StorageDispatchIntent, error) {
	if len(req.Payload) > 8*1024*1024 || uint64(len(req.Payload)) != req.ExpectedLength || len(req.CanonicalAuthorization) > 16*1024 ||
		len(req.ProtoReflect().GetUnknown()) != 0 || req.Precondition == nil || len(req.Precondition.ProtoReflect().GetUnknown()) != 0 ||
		(req.Fence != nil && len(req.Fence.ProtoReflect().GetUnknown()) != 0) {
		return store.StorageDispatchIntent{}, store.ErrInvalidStorageIntent
	}
	condition := ""
	switch req.Precondition.ConditionType {
	case actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_CREATE_ONLY:
		condition = "CREATE_ONLY"
	case actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_IF_MATCH:
		condition = "IF_MATCH"
	default:
		return store.StorageDispatchIntent{}, store.ErrInvalidStorageIntent
	}
	authorizationDigest := sha256.Sum256(req.CanonicalAuthorization)
	intent := store.StorageDispatchIntent{
		Schema: "storage-dispatch-intent-v1", ActionID: record.ID, ExecutionEpoch: record.ExecutionEpoch,
		OperationID: req.OperationId, RequestID: req.RequestId, Nonce: req.Nonce, MutationType: req.MutationType,
		Provider: req.Provider, TargetIdentity: req.TargetIdentity, Bucket: req.Bucket, Prefix: req.Prefix,
		ObjectKey: req.ObjectKey, PayloadDigest: req.PayloadDigest, ExpectedLength: req.ExpectedLength,
		Condition: condition, ExpectedETag: req.Precondition.ExpectedEtag,
		AuthorizationDigest: hex.EncodeToString(authorizationDigest[:]), RequestSchemaVersion: req.SchemaVersion,
	}
	return intent, store.ValidateStorageDispatchIntent(intent)
}
