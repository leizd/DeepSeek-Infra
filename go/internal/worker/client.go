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
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
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
	default:
		return ErrInvalidWorkerResponse
	}
}
