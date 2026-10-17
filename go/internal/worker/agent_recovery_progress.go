package worker

import (
	"context"
	"fmt"
	"math"

	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

type recoveryProgressClient interface {
	PrepareRecoveryProgress(context.Context, *agentv1.PrepareAgentRecoveryProgressRequest, ...grpc.CallOption) (*agentv1.AgentRecoveryProgressReceipt, error)
}

// Only public metadata crosses this boundary. Deployment-pinned signer identity
// and original task termination are checked again in Go's commit transaction.
func (client *Client) PrepareAgentRecoveryProgress(ctx context.Context, input *agentv1.PrepareAgentRecoveryProgressRequest) (*agentv1.AgentRecoveryProgressReceipt, error) {
	if input == nil || input.SchemaVersion != 1 || input.Context == nil || input.Fence == nil ||
		!agentproof.KnownRecoveryProgressMessage(input) {
		return nil, ErrInvalidWorkerResponse
	}
	scope := input.Context
	if scope.SchemaVersion != 1 || scope.RunId == "" || input.Fence.ActionId != scope.RunId || input.Fence.ExecutionEpoch != scope.MetadataEpoch ||
		scope.MetadataEpoch == 0 || scope.MetadataEpoch > math.MaxInt64 || scope.Generation == 0 || scope.Generation > math.MaxInt64 ||
		scope.ExpectedIndex < 1 || scope.ExpectedIndex > math.MaxInt64-2 || scope.RuntimeRevision < 2 || scope.RuntimeRevision > math.MaxInt64 ||
		!nodeOutcomeArtifact(scope.Request) || !nodeOutcomeHex(scope.RecoveryContextSha256) || scope.TaskActionId == "" || scope.TaskActionRevision < 1 ||
		scope.TaskMetadataIndex != scope.ExpectedIndex || !agentTaskRole(scope.Role) || len(scope.Outputs) != 1 {
		return nil, ErrInvalidWorkerResponse
	}
	outbound, err := client.agentNodeContext(ctx)
	if err != nil {
		return nil, err
	}
	rpc, ok := client.nodes.(recoveryProgressClient)
	if !ok {
		return nil, ErrInvalidWorkerResponse
	}
	result, err := rpc.PrepareRecoveryProgress(outbound, input)
	if err != nil {
		return nil, fmt.Errorf("worker recovery progress transport: %w", err)
	}
	if result == nil || result.Evidence == nil {
		return nil, ErrInvalidWorkerResponse
	}
	// This verifies integrity only. The store uses the configured public key,
	// fleet and environment rather than trusting the response's own identity.
	if agentproof.VerifyAgentRecoveryProgress(result, scope, agentproof.Binding{PublicKey: result.SignerPublicKey,
		FleetID: result.Evidence.FleetId, Environment: result.Evidence.Environment}) != nil || !proto.Equal(result.Evidence.Context, scope) {
		return nil, ErrInvalidWorkerResponse
	}
	return result, nil
}
