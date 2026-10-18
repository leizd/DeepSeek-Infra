package worker

import (
	"context"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc"
)

// agentNodeClient is the control plane's execution/projection surface. Private
// Rust credential custody is deliberately absent: Go never prepares credentials.
type agentNodeClient interface {
	ExecutePlan(context.Context, *agentv1.ExecuteAgentPlanRequest, ...grpc.CallOption) (*agentv1.AgentNodeEffectMetadata, error)
	ExecuteTask(context.Context, *agentv1.ExecuteAgentTaskRequest, ...grpc.CallOption) (*agentv1.AgentNodeEffectMetadata, error)
	ExecuteSynthesis(context.Context, *agentv1.ExecuteAgentTaskRequest, ...grpc.CallOption) (*agentv1.AgentNodeEffectMetadata, error)
	QueryEffect(context.Context, *agentv1.QueryAgentNodeEffectRequest, ...grpc.CallOption) (*agentv1.QueryAgentNodeEffectResponse, error)
	ReadRetainedEffect(context.Context, *agentv1.QueryAgentNodeEffectRequest, ...grpc.CallOption) (*agentv1.QueryAgentNodeEffectResponse, error)
	ProjectPlan(context.Context, *agentv1.ProjectAgentPlanRequest, ...grpc.CallOption) (*agentv1.AgentPlanProjectionReceipt, error)
	ProjectSynthesis(context.Context, *agentv1.ProjectAgentSynthesisRequest, ...grpc.CallOption) (*agentv1.AgentSynthesisProjectionReceipt, error)
	ProjectFailure(context.Context, *agentv1.ProjectAgentFailureRequest, ...grpc.CallOption) (*agentv1.AgentFailureProjectionReceipt, error)
}

var _ agentNodeClient = (agentv1.AgentNodeWorkerClient)(nil)
