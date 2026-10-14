package worker

import (
	"context"
	"fmt"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

// Projection performs no provider I/O. The response remains unverified until
// Go's settlement transaction checks its signature and current durable scope.
func (client *Client) ProjectAgentPlan(ctx context.Context, input *agentv1.ProjectAgentPlanRequest) (*agentv1.AgentPlanProjectionReceipt, error) {
	if input == nil || input.SchemaVersion != 1 || input.Source == nil || input.Source.Fence == nil || input.SettlementFence == nil ||
		input.Source.NodeId != "leader" || input.Source.Fence.ActionId != input.SettlementFence.ActionId || input.Source.RunId == "" ||
		input.Source.Fence.ExecutionEpoch == 0 || input.SettlementFence.ExecutionEpoch < input.Source.Fence.ExecutionEpoch ||
		input.SettlementWriterToken <= 0 || input.MetadataEpoch == 0 || input.ExpectedIndex < 0 || len(input.NormalizedPlan) == 0 ||
		(input.NextStatus != "running" && input.NextStatus != "awaiting_plan") {
		return nil, ErrInvalidWorkerResponse
	}
	outbound, err := client.agentNodeContext(ctx)
	if err != nil {
		return nil, err
	}
	result, err := client.nodes.ProjectPlan(outbound, input)
	if err != nil {
		return nil, fmt.Errorf("worker plan projection transport: %w", err)
	}
	if result == nil || result.SchemaVersion != 1 || result.Evidence == nil || len(result.ProtoReflect().GetUnknown()) != 0 ||
		!proto.Equal(result.Evidence.SettlementFence, input.SettlementFence) || result.Evidence.RunId != input.Source.RunId ||
		!nodeOutcomeArtifact(result.Evidence.PlanBody) || !nodeOutcomeArtifact(result.Evidence.StatusBody) {
		return nil, ErrInvalidWorkerResponse
	}
	return result, nil
}
