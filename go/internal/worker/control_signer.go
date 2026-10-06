package worker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

var ErrControlSignerResponse = errors.New("CONTROL_SIGNER_RESPONSE_INVALID")

type ControlSignerBinding struct{ PublicKey, FleetID, Environment string }

// Verifies the returned signature against deployment-pinned public metadata.
// This path never reads a bundle/passphrase or accepts a private signing key.
func (client *Client) SignControl(ctx context.Context, request *actionv1.SignControlRequest, binding ControlSignerBinding) ([]byte, error) {
	if ctx == nil || request == nil || internalprotocol.ValidateFence(request.Fence) != nil || request.FleetId != binding.FleetID || request.Environment != binding.Environment || request.FencingToken > uint64(^uint64(0)>>1) || len(request.ProtoReflect().GetUnknown()) != 0 || len(request.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, ErrControlSignerResponse
	}
	keyID, err := store.SignerKeyIDForPublicKey(binding.PublicKey)
	if err != nil {
		return nil, err
	}
	if client == nil || !client.tlsSecured || !client.bearerAttached {
		return nil, ErrWorkerTLSRequired
	}
	if client.signer == nil {
		return nil, ErrControlSignerResponse
	}
	outbound, err := client.outgoingContext(ctx, "")
	if err != nil {
		return nil, err
	}
	input := proto.Clone(request).(*actionv1.SignControlRequest)
	response, err := client.signer.SignControl(outbound, input)
	if err != nil {
		return nil, err
	}
	if response == nil || !proto.Equal(response.Fence, request.Fence) || response.SignerPublicKey != binding.PublicKey || response.SignerKeyId != keyID || len(response.CanonicalDocument) == 0 || len(response.ProtoReflect().GetUnknown()) != 0 {
		return nil, ErrControlSignerResponse
	}
	var document map[string]any
	now := time.Now().UTC()
	switch request.Purpose {
	case actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_INSTALL_EPOCH:
		document, err = store.VerifyAuthorityRequestDocument(response.CanonicalDocument, store.AuthorityRequestContext{Now: now, SignerPublicKey: binding.PublicKey, SignerKeyID: keyID, ExpectedDomain: "action", ExpectedOperation: "install-epoch", ExpectedRuntime: "go", ExpectedMode: "shadow", ExpectedFleetID: binding.FleetID, ExpectedEnvironment: binding.Environment, ExpectedRole: "control-plane", CurrentFencingToken: int64(request.FencingToken), MaxFutureSkewSeconds: 30})
	case actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_STORAGE_PUT:
		document, err = store.VerifyStorageOperationGrant(response.CanonicalDocument, store.StorageOperationGrantContext{Now: now, SignerPublicKey: binding.PublicKey, SignerKeyID: keyID, ExpectedDomain: "action", ExpectedOperation: store.StorageOperationGrantPut, ExpectedRuntime: "go", ExpectedMode: "shadow", ExpectedFleetID: binding.FleetID, ExpectedEnvironment: binding.Environment, ExpectedRole: "control-plane", CurrentFencingToken: int64(request.FencingToken), LiveEpoch: int64(request.Fence.ExecutionEpoch), MaxFutureSkewSeconds: 30})
		if err == nil {
			intent := request.StorageIntent
			if intent == nil || intent.Precondition == nil {
				return nil, ErrControlSignerResponse
			}
			condition := ""
			switch intent.Precondition.ConditionType {
			case actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_CREATE_ONLY:
				condition = "CREATE_ONLY"
			case actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_IF_MATCH:
				condition = "IF_MATCH"
			default:
				return nil, ErrControlSignerResponse
			}
			err = store.BindStorageOperationGrant(document, store.StorageOperationCommand{ActionID: request.Fence.ActionId, ExecutionEpoch: request.Fence.ExecutionEpoch, OperationID: intent.OperationId, MutationType: intent.MutationType, Provider: intent.Provider, TargetIdentity: intent.TargetIdentity, Bucket: intent.Bucket, Prefix: intent.Prefix, ObjectKey: intent.ObjectKey, ObjectDigest: intent.PayloadDigest, ExpectedLength: intent.ExpectedLength, ConditionType: condition, ExpectedETag: intent.Precondition.ExpectedEtag, ClaimRevision: request.Revision})
		}
	default:
		return nil, ErrControlSignerResponse
	}
	if err != nil {
		return nil, errors.Join(ErrControlSignerResponse, err)
	}
	// Full canonical verifiers check signature, scope and timestamps, and this
	// exact comparison additionally prevents a valid signature for another call.
	if document["actionId"] != request.Fence.ActionId || !signerIntegerMatches(document["executionEpoch"], int64(request.Fence.ExecutionEpoch)) || !signerIntegerMatches(document["revision"], request.Revision) || document["requestId"] != request.RequestId || document["nonce"] != request.Nonce || (request.IssuedAt != "" && document["issuedAt"] != request.IssuedAt) || (request.ExpiresAt != "" && document["expiresAt"] != request.ExpiresAt) {
		return nil, ErrControlSignerResponse
	}
	return append([]byte(nil), response.CanonicalDocument...), nil
}

func signerIntegerMatches(value any, expected int64) bool {
	number, ok := value.(json.Number)
	if !ok {
		return false
	}
	actual, err := number.Int64()
	return err == nil && actual == expected
}

func signerID(domain, value string) string {
	sum := sha256.Sum256([]byte(domain + "\x00" + value))
	return hex.EncodeToString(sum[:])
}

// AuthorizeStorage is used by the authoritative coordinator after its durable
// claim. Rust issues both frozen documents; only InstallAuthoritativeEpoch may
// install authority. A confirmed existing epoch can safely resume signing.
func (client *Client) AuthorizeStorage(ctx context.Context, request *actionv1.StorageMutationRequest, revision, fencingToken int64) (*actionv1.StorageMutationRequest, error) {
	if request == nil || request.Fence == nil || len(request.CanonicalAuthorization) != 0 || revision < 1 || fencingToken < 1 {
		return nil, ErrControlSignerResponse
	}
	binding := ControlSignerBinding{PublicKey: os.Getenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY"), FleetID: os.Getenv("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID"), Environment: os.Getenv("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT")}
	epochInput := &actionv1.SignControlRequest{Purpose: actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_INSTALL_EPOCH, Fence: proto.Clone(request.Fence).(*internalprotocol.ActionFence), Revision: revision, FencingToken: uint64(fencingToken), FleetId: binding.FleetID, Environment: binding.Environment, RequestId: signerID("epoch-request", request.RequestId), Nonce: signerID("epoch-nonce", request.Nonce)}
	raw, err := client.SignControl(ctx, epochInput, binding)
	if err != nil {
		return nil, err
	}
	if err = client.InstallAuthoritativeEpoch(ctx, request.Fence, raw); err != nil {
		if !errors.Is(err, store.ErrAuthorityRequestReplay) && !errors.Is(err, internalprotocol.ErrStaleEpoch) {
			return nil, err
		}
		if err = client.Admit(ctx, actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, request.Fence); err != nil {
			return nil, err
		}
	}
	intent := proto.Clone(request).(*actionv1.StorageMutationRequest)
	intent.Payload = nil
	grantInput := proto.Clone(epochInput).(*actionv1.SignControlRequest)
	grantInput.Purpose = actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_STORAGE_PUT
	grantInput.RequestId = request.RequestId
	grantInput.Nonce = request.Nonce
	grantInput.StorageIntent = intent
	raw, err = client.SignControl(ctx, grantInput, binding)
	if err != nil {
		return nil, err
	}
	authorized := proto.Clone(request).(*actionv1.StorageMutationRequest)
	authorized.CanonicalAuthorization = raw
	return authorized, nil
}
