package api

import (
	"context"
	"errors"

	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type agentNodeService struct {
	agentv1.UnimplementedAgentNodeControlServer
	run *agentRunService
}

func nodeDispatchProtocol(intent store.AgentNodeDispatch) *agentv1.AgentNodeDispatchIntent {
	phase := agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS
	if intent.Phase == "plan" {
		phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN
	} else if intent.Phase == "synthesis" {
		phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS
	}
	return &agentv1.AgentNodeDispatchIntent{SchemaVersion: intent.SchemaVersion, OperationId: intent.OperationID,
		Fence: &commonv1.ActionFence{ActionId: intent.ActionID, ExecutionEpoch: intent.ExecutionEpoch},
		RunId: intent.RunID, NodeId: intent.NodeID, Phase: phase, Task: intent.Task,
		DependsOn: append([]string(nil), intent.DependsOn...), Request: artifactProtocol(intent.Request),
		PlanDigest: intent.PlanDigest, MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch,
		State: intent.State, RecordedAt: intent.RecordedAt}
}

func nodeDispatchError(err error) error {
	if errors.Is(err, store.ErrAgentNodeDispatchRequired) || errors.Is(err, store.ErrAgentNodeOutcomeRequired) {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	if errors.Is(err, store.ErrAgentNodeOutcomeConflict) {
		return status.Error(codes.AlreadyExists, err.Error())
	}
	if errors.Is(err, store.ErrAgentNodeDependencyUnverified) || errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) || errors.Is(err, agentproof.ErrAgentPlanProjectionInvalid) || errors.Is(err, agentproof.ErrAgentSynthesisProjectionInvalid) || errors.Is(err, agentproof.ErrAgentFailureProjectionInvalid) {
		return status.Error(codes.FailedPrecondition, err.Error())
	}
	return executionWriteError(err)
}

func (service *agentNodeService) SettlePlan(ctx context.Context, input *agentv1.SettleAgentPlanRequest) (*agentv1.SettleAgentPlanResponse, error) {
	if err := service.run.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.Fence == nil || input.Projection == nil ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_PLAN_PROJECTION_INVALID")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.SettleAgentPlanRequest)
	result, err := service.run.control.SettleAgentPlan(store.AgentPlanSettlementRequest{OperationID: input.OperationId, Actor: input.Actor,
		ActionID: input.Fence.ActionId, Epoch: input.Fence.ExecutionEpoch, RunID: input.RunId, Owner: input.Owner, ClaimToken: input.ClaimToken, Projection: input.Projection})
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	return &agentv1.SettleAgentPlanResponse{AlreadyApplied: result.AlreadyApplied, Run: runProtocol(result.Run), ActionState: result.Action.State}, nil
}

func (service *agentNodeService) SettleFailure(ctx context.Context, input *agentv1.SettleAgentFailureRequest) (*agentv1.SettleAgentFailureResponse, error) {
	if err := service.run.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.Fence == nil || input.Projection == nil ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_FAILURE_PROJECTION_INVALID")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.SettleAgentFailureRequest)
	result, err := service.run.control.SettleAgentFailure(store.AgentFailureSettlementRequest{OperationID: input.OperationId, Actor: input.Actor,
		ActionID: input.Fence.ActionId, Epoch: input.Fence.ExecutionEpoch, RunID: input.RunId, Owner: input.Owner, ClaimToken: input.ClaimToken, Projection: input.Projection})
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	return &agentv1.SettleAgentFailureResponse{AlreadyApplied: result.AlreadyApplied, Run: runProtocol(result.Run), ActionState: result.Action.State}, nil
}

func (service *agentNodeService) SettleSynthesis(ctx context.Context, input *agentv1.SettleAgentSynthesisRequest) (*agentv1.SettleAgentSynthesisResponse, error) {
	if err := service.run.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.Fence == nil || input.Projection == nil ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_SYNTHESIS_PROJECTION_INVALID")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.SettleAgentSynthesisRequest)
	result, err := service.run.control.SettleAgentSynthesis(store.AgentSynthesisSettlementRequest{OperationID: input.OperationId, Actor: input.Actor,
		ActionID: input.Fence.ActionId, Epoch: input.Fence.ExecutionEpoch, RunID: input.RunId, Owner: input.Owner, ClaimToken: input.ClaimToken, Projection: input.Projection})
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	return &agentv1.SettleAgentSynthesisResponse{AlreadyApplied: result.AlreadyApplied, Run: runProtocol(result.Run), ActionState: result.Action.State}, nil
}

func (service *agentNodeService) BeginDispatch(ctx context.Context, input *agentv1.BeginAgentNodeDispatchRequest) (*agentv1.BeginAgentNodeDispatchResponse, error) {
	if err := service.run.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.Fence == nil ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_NODE_DISPATCH_REQUIRED")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.BeginAgentNodeDispatchRequest)
	result, err := service.run.control.BeginAgentNodeDispatch(store.AgentNodeDispatchRequest{OperationID: input.OperationId,
		Actor: input.Actor, ActionID: input.Fence.ActionId, Epoch: input.Fence.ExecutionEpoch,
		RunID: input.RunId, NodeID: input.NodeId, Owner: input.Owner, ClaimToken: input.ClaimToken})
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	claim, err := executionClaimProtocol(result.Admission, result.WriterLeaseUntil)
	if err != nil {
		return nil, err
	}
	return &agentv1.BeginAgentNodeDispatchResponse{Fresh: result.Fresh, Intent: nodeDispatchProtocol(result.Intent), Claim: claim}, nil
}

func (service *agentNodeService) GetDispatch(ctx context.Context, input *agentv1.GetAgentNodeDispatchRequest) (*agentv1.GetAgentNodeDispatchResponse, error) {
	if err := service.run.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.Fence == nil || !store.ValidRecordID(input.Fence.ActionId) || input.Fence.ExecutionEpoch == 0 ||
		!store.ValidRecordID(input.RunId) || input.NodeId == "" || len(input.NodeId) > 64 ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_NODE_DISPATCH_REQUIRED")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	_, authoritative, err := service.run.authority()
	if err != nil {
		return nil, err
	}
	cutover, err := service.run.control.GetCutover("action")
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	if !authoritative || !store.IsDomainGoAuthoritative(cutover.State) {
		return nil, status.Error(codes.FailedPrecondition, "CUTOVER_NOT_AUTHORIZED")
	}
	record, intent, receipt, found, err := service.run.control.AgentNodeOutcomeSnapshot(input.Fence.ActionId, input.NodeId)
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	if record.ID == "" {
		return nil, status.Error(codes.NotFound, "AGENT_EXECUTION_NOT_FOUND")
	}
	binding, err := store.AgentRunExecutionBindingFromRecord(record)
	if err != nil || binding.RunID != input.RunId {
		return nil, status.Error(codes.FailedPrecondition, "FENCE_MISMATCH")
	}
	if record.ExecutionEpoch != input.Fence.ExecutionEpoch {
		return nil, status.Error(codes.FailedPrecondition, "STALE_EXECUTION_EPOCH")
	}
	result := &agentv1.GetAgentNodeDispatchResponse{Found: found}
	if found {
		result.Intent = nodeDispatchProtocol(intent)
		result.OutcomeReceipt = receipt
	}
	return result, nil
}

// Receipt ingestion preserves historical evidence without granting another send.
func (service *agentNodeService) RecordOutcome(ctx context.Context, input *agentv1.RecordAgentNodeOutcomeRequest) (*agentv1.RecordAgentNodeOutcomeResponse, error) {
	if err := service.run.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.Fence == nil || input.OutcomeReceipt == nil ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_NODE_OUTCOME_REQUIRED")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.RecordAgentNodeOutcomeRequest)
	result, err := service.run.control.RecordAgentNodeOutcome(store.AgentNodeOutcomeRequest{
		OperationID: input.OperationId, Actor: input.Actor, ActionID: input.Fence.ActionId, Epoch: input.Fence.ExecutionEpoch,
		RunID: input.RunId, NodeID: input.NodeId, Receipt: input.OutcomeReceipt,
	})
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	return &agentv1.RecordAgentNodeOutcomeResponse{AlreadyRecorded: result.AlreadyRecorded, ReceiptSha256: result.ReceiptSHA256}, nil
}

func (service *agentNodeService) SettleTask(ctx context.Context, input *agentv1.SettleAgentTaskRequest) (*agentv1.SettleAgentTaskResponse, error) {
	if err := service.run.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.Fence == nil || len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_TASK_SETTLEMENT_REQUIRED")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.SettleAgentTaskRequest)
	result, err := service.run.control.SettleAgentTask(store.AgentTaskSettlementRequest{OperationID: input.OperationId, Actor: input.Actor, ActionID: input.Fence.ActionId,
		Epoch: input.Fence.ExecutionEpoch, RunID: input.RunId, NodeID: input.NodeId, Owner: input.Owner, ClaimToken: input.ClaimToken, ExpectedIndex: input.ExpectedIndex, SourceReceiptSHA256: input.SourceReceiptSha256})
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	return &agentv1.SettleAgentTaskResponse{AlreadyApplied: result.AlreadyApplied, Run: runProtocol(result.Run), ActionState: result.Action.State}, nil
}

func (service *agentNodeService) CompleteTasks(ctx context.Context, input *agentv1.CompleteAgentTasksRequest) (*agentv1.CompleteAgentTasksResponse, error) {
	if err := service.run.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.Fence == nil ||
		len(input.ProtoReflect().GetUnknown()) != 0 || len(input.Fence.ProtoReflect().GetUnknown()) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_NODE_OUTCOME_REQUIRED")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	input = proto.Clone(input).(*agentv1.CompleteAgentTasksRequest)
	result, err := service.run.control.CompleteAgentTasks(store.CompleteAgentTasksRequest{OperationID: input.OperationId, Actor: input.Actor,
		ActionID: input.Fence.ActionId, Epoch: input.Fence.ExecutionEpoch, RunID: input.RunId, Owner: input.Owner,
		ClaimToken: input.ClaimToken, ExpectedIndex: input.ExpectedIndex})
	if err != nil {
		return nil, nodeDispatchError(err)
	}
	return &agentv1.CompleteAgentTasksResponse{AlreadyApplied: result.AlreadyApplied, Run: runProtocol(result.Run), ActionState: result.Action.State}, nil
}
