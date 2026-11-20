package worker

import (
	"context"
	"fmt"
	"math"
	"strings"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

// This derives signed public bodies without provider I/O. The caller must still
// verify the receipt against Go's durable source and settle in its own transaction.
func (client *Client) ProjectAgentFailure(ctx context.Context, input *agentv1.ProjectAgentFailureRequest) (*agentv1.AgentFailureProjectionReceipt, error) {
	if input == nil || input.SchemaVersion != 1 || input.Source == nil || input.Source.Fence == nil || input.SettlementFence == nil ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Source.ProtoReflect().GetUnknown()) != 0 ||
		len(input.Source.Fence.ProtoReflect().GetUnknown()) != 0 || len(input.SettlementFence.ProtoReflect().GetUnknown()) != 0 ||
		(input.Source.NodeId != "leader" && input.Source.NodeId != "synthesizer") || input.Source.RunId == "" || input.Source.Fence.ActionId != input.SettlementFence.ActionId ||
		input.Source.Fence.ExecutionEpoch == 0 || input.SettlementFence.ExecutionEpoch < input.Source.Fence.ExecutionEpoch ||
		!strings.HasPrefix(input.Source.OperationId, "agent-node-") || len(input.Source.OperationId) != 75 || !nodeOutcomeHex(input.Source.OperationId[11:]) ||
		!nodeOutcomeHex(input.Source.ExpectedReceiptSha256) || input.SettlementWriterToken <= 0 || input.MetadataEpoch == 0 || input.ExpectedIndex < 0 || input.ExpectedIndex == math.MaxInt64 {
		return nil, ErrInvalidWorkerResponse
	}
	outbound, err := client.agentNodeContext(ctx)
	if err != nil {
		return nil, err
	}
	result, err := client.nodes.ProjectFailure(outbound, input)
	if err != nil {
		return nil, fmt.Errorf("worker failure projection transport: %w", err)
	}
	if result == nil || result.SchemaVersion != 1 || result.Evidence == nil || len(result.ProtoReflect().GetUnknown()) != 0 ||
		result.Evidence.SchemaVersion != 1 || len(result.Evidence.ProtoReflect().GetUnknown()) != 0 ||
		result.Evidence.SourceReceiptSha256 != input.Source.ExpectedReceiptSha256 ||
		!proto.Equal(result.Evidence.SettlementFence, input.SettlementFence) || result.Evidence.SettlementWriterToken != input.SettlementWriterToken ||
		result.Evidence.RunId != input.Source.RunId || result.Evidence.MetadataEpoch != input.MetadataEpoch || result.Evidence.ExpectedIndex != input.ExpectedIndex ||
		!nodeOutcomeArtifact(result.Evidence.ErrorBody) ||
		(result.Evidence.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN && result.Evidence.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS) ||
		(result.Evidence.Phase == agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN) != (input.Source.NodeId == "leader") {
		return nil, ErrInvalidWorkerResponse
	}
	return result, nil
}
