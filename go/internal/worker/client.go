package worker

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strconv"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

var (
	ErrInvalidWorkerResponse = errors.New("WORKER_RESPONSE_INVALID")
	ErrUnsafeWorkerTarget    = errors.New("WORKER_TARGET_UNSAFE")
)

type Client struct {
	rpc        actionv1.WorkerClient
	connection *grpc.ClientConn
}

func New(rpc actionv1.WorkerClient) *Client {
	return &Client{rpc: rpc}
}

func DialPlaintextLoopback(target string) (*Client, error) {
	if err := ValidatePlaintextTarget(target); err != nil {
		return nil, err
	}
	connection, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("create worker client: %w", err)
	}
	return &Client{rpc: actionv1.NewWorkerClient(connection), connection: connection}, nil
}

func (client *Client) Close() error {
	if client == nil || client.connection == nil {
		return nil
	}
	return client.connection.Close()
}

func (client *Client) Admit(ctx context.Context, kind actionv1.CommandKind, fence *commonv1.ActionFence) error {
	if err := internalprotocol.ValidateFence(fence); err != nil {
		return err
	}
	if !internalprotocol.IsStorageCommand(kind) && !internalprotocol.IsTransferCommand(kind) && !internalprotocol.IsFederationCommand(kind) {
		return internalprotocol.ErrUnknownEffect
	}
	if client == nil || client.rpc == nil {
		return ErrInvalidWorkerResponse
	}
	response, err := client.rpc.AdmitCommand(ctx, &actionv1.AdmitCommandRequest{
		Kind:  kind,
		Fence: fence,
		// live_epoch is intentionally omitted. Only the Rust worker's local
		// authority state may decide whether this command is current.
	})
	if err != nil {
		return fmt.Errorf("worker admission transport: %w", err)
	}
	if response == nil {
		return ErrInvalidWorkerResponse
	}
	switch response.Status {
	case actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED:
		if response.State != commonv1.EffectState_EFFECT_STATE_UNKNOWN || response.Error != nil {
			return ErrInvalidWorkerResponse
		}
		return nil
	case actionv1.AdmitStatus_ADMIT_STATUS_REJECTED:
		if response.State != commonv1.EffectState_EFFECT_STATE_UNKNOWN || response.Error == nil {
			return ErrInvalidWorkerResponse
		}
		return knownRejection(response.Error.Code)
	default:
		return ErrInvalidWorkerResponse
	}
}

func (client *Client) InstallAuthoritativeEpoch(ctx context.Context, fence *commonv1.ActionFence, canonical []byte) error {
	if err := internalprotocol.ValidateFence(fence); err != nil {
		return err
	}
	if len(canonical) == 0 {
		return store.ErrAuthorityRequestInvalid
	}
	if len(canonical) > store.MaxAuthorityRequestBytes {
		return store.ErrAuthorityRequestTooLarge
	}
	parsed, err := store.FenceFromAuthorityRequest(canonical)
	if err != nil {
		return err
	}
	if parsed.ActionId != fence.ActionId || parsed.ExecutionEpoch != fence.ExecutionEpoch {
		return internalprotocol.ErrFenceMismatch
	}
	if client == nil || client.rpc == nil {
		return ErrInvalidWorkerResponse
	}
	response, err := client.rpc.InstallAuthoritativeEpoch(ctx, &actionv1.InstallAuthoritativeEpochRequest{
		Fence:            fence,
		CanonicalRequest: canonical,
	})
	if err != nil {
		return fmt.Errorf("worker authority transport: %w", err)
	}
	if response == nil {
		return ErrInvalidWorkerResponse
	}
	switch response.Status {
	case actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED:
		if response.Error != nil || response.Fence == nil ||
			response.Fence.ActionId != fence.ActionId ||
			response.Fence.ExecutionEpoch != fence.ExecutionEpoch {
			return ErrInvalidWorkerResponse
		}
		return nil
	case actionv1.AdmitStatus_ADMIT_STATUS_REJECTED:
		if response.Fence != nil || response.Error == nil {
			return ErrInvalidWorkerResponse
		}
		return knownAuthorityRejection(response.Error.Code)
	default:
		return ErrInvalidWorkerResponse
	}
}

func (client *Client) QueryEffect(ctx context.Context, fence *commonv1.ActionFence) (commonv1.EffectState, error) {
	if err := internalprotocol.ValidateFence(fence); err != nil {
		return commonv1.EffectState_EFFECT_STATE_UNKNOWN, err
	}
	if client == nil || client.rpc == nil {
		return commonv1.EffectState_EFFECT_STATE_UNKNOWN, ErrInvalidWorkerResponse
	}
	response, err := client.rpc.QueryEffect(ctx, &actionv1.QueryEffectRequest{Fence: fence})
	if err != nil {
		return commonv1.EffectState_EFFECT_STATE_UNKNOWN, fmt.Errorf("worker effect query transport: %w", err)
	}
	if response == nil || response.Fence == nil ||
		response.Fence.ActionId != fence.ActionId ||
		response.Fence.ExecutionEpoch != fence.ExecutionEpoch ||
		response.State != commonv1.EffectState_EFFECT_STATE_UNKNOWN ||
		response.Error == nil ||
		response.EffectId != "" ||
		response.ReceiptDigest != "" ||
		response.CommitDigest != "" ||
		response.ProofDigest != "" {
		return commonv1.EffectState_EFFECT_STATE_UNKNOWN, ErrInvalidWorkerResponse
	}
	rejection := knownRejection(response.Error.Code)
	if rejection != internalprotocol.ErrUnknownEffect && rejection != internalprotocol.ErrProofNotAuthoritative {
		return commonv1.EffectState_EFFECT_STATE_UNKNOWN, ErrInvalidWorkerResponse
	}
	return commonv1.EffectState_EFFECT_STATE_UNKNOWN, rejection
}

func (client *Client) ExecuteStorageMutation(ctx context.Context, request *actionv1.StorageMutationRequest, bearerToken string) (*actionv1.StorageMutationResponse, error) {
	if client == nil || client.rpc == nil {
		return nil, ErrInvalidWorkerResponse
	}
	if request == nil || request.Fence == nil {
		return nil, internalprotocol.ErrEmptyActionID
	}
	if err := internalprotocol.ValidateFence(request.Fence); err != nil {
		return nil, err
	}
	if request.OperationId == "" {
		return nil, store.ErrAuthorityRequestOperationInvalid
	}

	callCtx := ctx
	if bearerToken != "" {
		callCtx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+bearerToken)
	}

	response, err := client.rpc.ExecuteStorageMutation(callCtx, request)
	if err != nil {
		return nil, fmt.Errorf("worker storage mutation transport: %w", err)
	}
	if response == nil {
		return nil, ErrInvalidWorkerResponse
	}

	if response.Fence != nil {
		if response.Fence.ActionId != request.Fence.ActionId || response.Fence.ExecutionEpoch != request.Fence.ExecutionEpoch {
			return nil, ErrInvalidWorkerResponse
		}
	}
	if response.OperationId != "" && response.OperationId != request.OperationId {
		return nil, ErrInvalidWorkerResponse
	}

	switch response.Status {
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED:
		if response.Error != nil || response.State != commonv1.EffectState_EFFECT_STATE_APPLIED {
			return nil, ErrInvalidWorkerResponse
		}
		return response, nil
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN:
		return response, internalprotocol.ErrUnknownEffect
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED:
		if response.Error == nil {
			return nil, ErrInvalidWorkerResponse
		}
		return response, knownRejection(response.Error.Code)
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED:
		if response.Error == nil {
			return nil, ErrInvalidWorkerResponse
		}
		return response, knownRejection(response.Error.Code)
	default:
		return nil, ErrInvalidWorkerResponse
	}
}

func (client *Client) QueryStorageEffect(ctx context.Context, fence *commonv1.ActionFence, operationID string, bearerToken string) (*actionv1.StorageMutationResponse, error) {
	if client == nil || client.rpc == nil {
		return nil, ErrInvalidWorkerResponse
	}
	if fence == nil {
		return nil, internalprotocol.ErrEmptyActionID
	}
	if err := internalprotocol.ValidateFence(fence); err != nil {
		return nil, err
	}

	callCtx := ctx
	if bearerToken != "" {
		callCtx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+bearerToken)
	}

	response, err := client.rpc.QueryStorageEffect(callCtx, &actionv1.QueryStorageEffectRequest{
		Fence:       fence,
		OperationId: operationID,
	})
	if err != nil {
		return nil, fmt.Errorf("worker storage query transport: %w", err)
	}
	if response == nil {
		return nil, ErrInvalidWorkerResponse
	}

	if response.Fence != nil {
		if response.Fence.ActionId != fence.ActionId || response.Fence.ExecutionEpoch != fence.ExecutionEpoch {
			return nil, ErrInvalidWorkerResponse
		}
	}

	switch response.Status {
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED:
		if response.Error != nil || response.State != commonv1.EffectState_EFFECT_STATE_APPLIED {
			return nil, ErrInvalidWorkerResponse
		}
		return response, nil
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN:
		return response, internalprotocol.ErrUnknownEffect
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_RECONCILING:
		return response, internalprotocol.ErrUnknownEffect
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED:
		if response.Error == nil {
			return nil, ErrInvalidWorkerResponse
		}
		return response, knownRejection(response.Error.Code)
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED:
		if response.Error == nil {
			return nil, ErrInvalidWorkerResponse
		}
		return response, knownRejection(response.Error.Code)
	default:
		return nil, ErrInvalidWorkerResponse
	}
}

func ValidatePlaintextTarget(target string) error {
	host, port, err := net.SplitHostPort(target)
	if err != nil || host == "" || port == "" {
		return ErrUnsafeWorkerTarget
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return ErrUnsafeWorkerTarget
	}
	parsedPort, err := strconv.ParseUint(port, 10, 16)
	if err != nil || parsedPort == 0 {
		return ErrUnsafeWorkerTarget
	}
	return nil
}

func knownRejection(code string) error {
	switch code {
	case internalprotocol.ErrEmptyActionID.Error():
		return internalprotocol.ErrEmptyActionID
	case internalprotocol.ErrZeroEpoch.Error():
		return internalprotocol.ErrZeroEpoch
	case internalprotocol.ErrStaleEpoch.Error():
		return internalprotocol.ErrStaleEpoch
	case internalprotocol.ErrFenceMismatch.Error():
		return internalprotocol.ErrFenceMismatch
	case internalprotocol.ErrUnknownEffect.Error():
		return internalprotocol.ErrUnknownEffect
	case internalprotocol.ErrStorageNotAuthoritative.Error():
		return internalprotocol.ErrStorageNotAuthoritative
	case internalprotocol.ErrTransferNotAuthoritative.Error():
		return internalprotocol.ErrTransferNotAuthoritative
	case internalprotocol.ErrFederationNotAuthoritative.Error():
		return internalprotocol.ErrFederationNotAuthoritative
	case internalprotocol.ErrProofNotAuthoritative.Error():
		return internalprotocol.ErrProofNotAuthoritative
	case internalprotocol.ErrServiceAuthenticationUnavailable.Error():
		return internalprotocol.ErrServiceAuthenticationUnavailable
	case internalprotocol.ErrAuthenticationMissing.Error():
		return internalprotocol.ErrAuthenticationMissing
	case internalprotocol.ErrAuthenticationInvalid.Error():
		return internalprotocol.ErrAuthenticationInvalid
	case internalprotocol.ErrStoragePreconditionRejected.Error():
		return internalprotocol.ErrStoragePreconditionRejected
	case internalprotocol.ErrStorageReplayRejected.Error():
		return internalprotocol.ErrStorageReplayRejected
	case internalprotocol.ErrStorageUnknownEffectRetryBlocked.Error():
		return internalprotocol.ErrStorageUnknownEffectRetryBlocked
	case internalprotocol.ErrStorageTargetMismatch.Error():
		return internalprotocol.ErrStorageTargetMismatch
	case internalprotocol.ErrStorageDigestMismatch.Error():
		return internalprotocol.ErrStorageDigestMismatch
	case internalprotocol.ErrStorageWorkerWithoutAuthority.Error():
		return internalprotocol.ErrStorageWorkerWithoutAuthority
	case internalprotocol.ErrStorageTransportUnavailable.Error():
		return internalprotocol.ErrStorageTransportUnavailable
	case internalprotocol.ErrStorageTransportError.Error():
		return internalprotocol.ErrStorageTransportError
	case store.ErrAuthorityRequestOperationInvalid.Error():
		return store.ErrAuthorityRequestOperationInvalid
	default:
		return ErrInvalidWorkerResponse
	}
}

func knownAuthorityRejection(code string) error {
	switch code {
	case store.ErrAuthorityRequestInvalid.Error():
		return store.ErrAuthorityRequestInvalid
	case store.ErrAuthorityRequestTooLarge.Error():
		return store.ErrAuthorityRequestTooLarge
	case store.ErrAuthorityRequestSchemaInvalid.Error():
		return store.ErrAuthorityRequestSchemaInvalid
	case store.ErrAuthorityRequestFieldsInvalid.Error():
		return store.ErrAuthorityRequestFieldsInvalid
	case store.ErrAuthorityRequestCanonicalMismatch.Error():
		return store.ErrAuthorityRequestCanonicalMismatch
	case store.ErrAuthorityRequestDigestMismatch.Error():
		return store.ErrAuthorityRequestDigestMismatch
	case store.ErrAuthorityRequestPayloadDigestMismatch.Error():
		return store.ErrAuthorityRequestPayloadDigestMismatch
	case store.ErrAuthorityRequestSignatureInvalid.Error():
		return store.ErrAuthorityRequestSignatureInvalid
	case store.ErrAuthorityRequestExpired.Error():
		return store.ErrAuthorityRequestExpired
	case store.ErrAuthorityRequestFutureSkew.Error():
		return store.ErrAuthorityRequestFutureSkew
	case store.ErrAuthorityRequestReplay.Error():
		return store.ErrAuthorityRequestReplay
	case store.ErrAuthorityRequestNonceReuse.Error():
		return store.ErrAuthorityRequestNonceReuse
	case store.ErrAuthorityRequestDomainMismatch.Error():
		return store.ErrAuthorityRequestDomainMismatch
	case store.ErrAuthorityRequestFleetMismatch.Error():
		return store.ErrAuthorityRequestFleetMismatch
	case store.ErrAuthorityRequestEnvironmentMismatch.Error():
		return store.ErrAuthorityRequestEnvironmentMismatch
	case store.ErrAuthorityRequestRoleMismatch.Error():
		return store.ErrAuthorityRequestRoleMismatch
	case store.ErrAuthorityRequestRuntimeMismatch.Error():
		return store.ErrAuthorityRequestRuntimeMismatch
	case store.ErrAuthorityRequestModeMismatch.Error():
		return store.ErrAuthorityRequestModeMismatch
	case store.ErrAuthorityRequestOperationInvalid.Error():
		return store.ErrAuthorityRequestOperationInvalid
	case store.ErrAuthorityRequestStaleFencingToken.Error():
		return store.ErrAuthorityRequestStaleFencingToken
	case store.ErrAuthorityRequestSignerMismatch.Error():
		return store.ErrAuthorityRequestSignerMismatch
	case store.ErrAuthorityRequestSecretDetected.Error():
		return store.ErrAuthorityRequestSecretDetected
	case internalprotocol.ErrEmptyActionID.Error():
		return internalprotocol.ErrEmptyActionID
	case internalprotocol.ErrZeroEpoch.Error():
		return internalprotocol.ErrZeroEpoch
	case internalprotocol.ErrStaleEpoch.Error():
		return internalprotocol.ErrStaleEpoch
	case internalprotocol.ErrFenceMismatch.Error():
		return internalprotocol.ErrFenceMismatch
	default:
		return ErrInvalidWorkerResponse
	}
}
