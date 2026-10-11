package worker

import (
	"context"
	"fmt"
	"strings"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func (client *Client) agentNodeContext(ctx context.Context) (context.Context, error) {
	if client == nil || client.nodes == nil || !client.tlsSecured || !client.bearerAttached {
		return nil, ErrWorkerTLSRequired
	}
	return client.outgoingContext(ctx, "")
}

func validAgentNodeEffect(effect *agentv1.AgentNodeEffectMetadata, fence *commonv1.ActionFence, operation, run, node string) bool {
	if effect == nil || len(effect.ProtoReflect().GetUnknown()) != 0 || !proto.Equal(effect.Fence, fence) ||
		effect.RunId != run || effect.NodeId != node || effect.OperationId != operation {
		return false
	}
	switch effect.State {
	case "CONFIRMED":
		return nodeOutcomeArtifact(effect.PublicEvent) && nodeOutcomeHex(effect.OutcomeSha256) &&
			effect.OutcomeLength > 0 && effect.OutcomeLength <= 1024*1024 && effect.OutcomeReceipt != nil
	case "EFFECT_UNKNOWN":
		return effect.PublicEvent == nil && effect.OutcomeSha256 == "" && effect.OutcomeLength == 0 && effect.OutcomeReceipt == nil
	default:
		return false
	}
}

// A confirmed response is still unverified until VerifyAgentNodeEffect checks
// it against the durable Go reservation and pinned Rust custody public key.
func (client *Client) ExecuteAgentPlan(ctx context.Context, request *agentv1.ExecuteAgentPlanRequest) (*agentv1.AgentNodeEffectMetadata, error) {
	if request == nil || request.SchemaVersion != 1 || request.OperationId == "" || len(request.OperationId) > 256 ||
		request.Actor != "rust-agent-worker" || request.Claim == nil || request.Claim.RunId == "" ||
		request.Claim.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN ||
		request.Fence == nil || request.Fence.ActionId == "" || request.Fence.ExecutionEpoch == 0 ||
		!proto.Equal(request.Fence, request.Claim.Fence) || len(request.SignedPhaseGrant) == 0 || len(request.SignedPhaseGrant) > 16384 {
		return nil, ErrInvalidWorkerResponse
	}
	outbound, err := client.agentNodeContext(ctx)
	if err != nil {
		return nil, err
	}
	result, err := client.nodes.ExecutePlan(outbound, request)
	if err != nil {
		return nil, fmt.Errorf("worker planner transport: %w", err)
	}
	if result == nil || !strings.HasPrefix(result.OperationId, "agent-node-") || len(result.OperationId) != 75 ||
		!nodeOutcomeHex(result.OperationId[11:]) || result.State != "CONFIRMED" ||
		!validAgentNodeEffect(result, request.Fence, result.OperationId, request.Claim.RunId, "leader") {
		return nil, ErrInvalidWorkerResponse
	}
	return result, nil
}

// Historical queries never request a fresh dispatch or infer an unapplied
// effect from absence. An unknown row carries no result or success receipt.
func (client *Client) QueryAgentNodeEffect(ctx context.Context, query *agentv1.QueryAgentNodeEffectRequest) (*agentv1.AgentNodeEffectMetadata, error) {
	if query == nil || query.Fence == nil || query.Fence.ActionId == "" || query.Fence.ExecutionEpoch == 0 ||
		query.RunId == "" || query.NodeId == "" || !strings.HasPrefix(query.OperationId, "agent-node-") ||
		len(query.OperationId) != 75 || !nodeOutcomeHex(query.OperationId[11:]) {
		return nil, ErrInvalidWorkerResponse
	}
	outbound, err := client.agentNodeContext(ctx)
	if err != nil {
		return nil, err
	}
	result, err := client.nodes.QueryEffect(outbound, query)
	if err != nil {
		return nil, fmt.Errorf("worker node query transport: %w", err)
	}
	if result == nil || len(result.ProtoReflect().GetUnknown()) != 0 || (!result.Found && result.Effect != nil) ||
		(result.Found && !validAgentNodeEffect(result.Effect, query.Fence, query.OperationId, query.RunId, query.NodeId)) {
		return nil, ErrInvalidWorkerResponse
	}
	return result.Effect, nil
}
