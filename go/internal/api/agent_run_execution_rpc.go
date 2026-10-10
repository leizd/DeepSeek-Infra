package api

import (
	"context"
	"errors"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (service *agentRunService) executionProcess() error {
	if !service.processAuthoritative() {
		return status.Error(codes.PermissionDenied, "MUTATION_DENIED")
	}
	if service.control == nil {
		return status.Error(codes.Unavailable, "CONTROL_STORE_UNAVAILABLE")
	}
	return nil
}

func executionPhaseName(phase agentv1.AgentRunExecutionPhase) string {
	switch phase {
	case agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN:
		return "plan"
	case agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS:
		return "tasks"
	default:
		return ""
	}
}

func executionClaimProtocol(admission store.AdmissionResult, writerLeaseUntil int64) (*agentv1.AgentRunExecutionClaim, error) {
	binding, err := store.AgentRunExecutionBindingFromRecord(admission.Record)
	if err != nil {
		return nil, status.Error(codes.DataLoss, "AGENT_EXECUTION_BINDING_INVALID")
	}
	phase := agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS
	if binding.Phase == "plan" {
		phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN
	}
	claim := admission.Lease
	if claim.ActionID != admission.Record.ID || claim.Epoch != admission.Record.ExecutionEpoch ||
		claim.Epoch == 0 || writerLeaseUntil <= 0 || claim.WriterFencingToken <= 0 || claim.Owner == "" || claim.ClaimToken == "" {
		return nil, status.Error(codes.DataLoss, "AGENT_EXECUTION_CLAIM_INVALID")
	}
	return &agentv1.AgentRunExecutionClaim{Fence: &commonv1.ActionFence{ActionId: claim.ActionID, ExecutionEpoch: claim.Epoch},
		RunId: binding.RunID, Phase: phase, Request: artifactProtocol(binding.Request), PlanDigest: binding.PlanDigest,
		MetadataIndex: binding.MetadataIndex, MetadataEpoch: binding.MetadataEpoch, Owner: claim.Owner, ClaimToken: claim.ClaimToken,
		LeaseUntil: claim.LeaseUntil, ClaimRevision: claim.ClaimRevision, WriterFencingToken: claim.WriterFencingToken,
		WriterLeaseUntil: writerLeaseUntil, State: admission.Record.State,
		ReconciliationRequired: admission.Record.State != "CLAIMED" && admission.Record.State != "EXECUTING"}, nil
}

func executionWriteError(err error) error {
	switch {
	case errors.Is(err, store.ErrAgentRunExecutionRequired):
		return status.Error(codes.InvalidArgument, "AGENT_RUN_EXECUTION_INVALID")
	case errors.Is(err, store.ErrActionNotClaimable), errors.Is(err, store.ErrActionLeaseActive),
		errors.Is(err, store.ErrActionLeaseStale), errors.Is(err, store.ErrActionLeaseExpired),
		errors.Is(err, store.ErrActionLeaseNotFound), errors.Is(err, store.ErrInvalidClaimToken):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, store.ErrResourceConflict), errors.Is(err, store.ErrBudgetExceeded):
		return status.Error(codes.ResourceExhausted, err.Error())
	default:
		return agentMetadataWriteError(err)
	}
}

func (service *agentRunService) ClaimExecution(ctx context.Context, input *agentv1.ClaimAgentRunExecutionRequest) (*agentv1.ClaimAgentRunExecutionResponse, error) {
	if err := service.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || executionPhaseName(input.Phase) == "" {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_EXECUTION_INVALID")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	result, err := service.control.ClaimAgentRunExecution(store.AgentRunExecutionRequest{OperationID: input.OperationId,
		Actor: input.Actor, RunID: input.RunId, Phase: executionPhaseName(input.Phase), ExpectedIndex: input.ExpectedIndex,
		Fence: input.MetadataFence, Owner: input.Owner, LeaseSeconds: int64(input.LeaseSeconds)})
	if err != nil {
		return nil, executionWriteError(err)
	}
	claim, err := executionClaimProtocol(result.Admission, result.WriterLeaseUntil)
	if err != nil {
		return nil, err
	}
	return &agentv1.ClaimAgentRunExecutionResponse{AlreadyApplied: result.Mutation.Status == store.OperatorMutationAlreadyApplied, Claim: claim}, nil
}

func (service *agentRunService) RenewExecution(ctx context.Context, input *agentv1.RenewAgentRunExecutionRequest) (*agentv1.RenewAgentRunExecutionResponse, error) {
	if err := service.executionProcess(); err != nil {
		return nil, err
	}
	if input == nil || input.Fence == nil || !store.ValidRecordID(input.Fence.ActionId) || input.Fence.ExecutionEpoch == 0 ||
		!store.ValidRecordID(input.RunId) || input.Owner == "" || input.ClaimToken == "" || input.LeaseSeconds > 300 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_EXECUTION_INVALID")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	record, found, err := service.control.Get("action", input.Fence.ActionId)
	if err != nil {
		return nil, executionWriteError(err)
	}
	if !found {
		return nil, status.Error(codes.NotFound, "AGENT_EXECUTION_NOT_FOUND")
	}
	binding, err := store.AgentRunExecutionBindingFromRecord(record)
	if err != nil || binding.RunID != input.RunId {
		return nil, status.Error(codes.FailedPrecondition, "FENCE_MISMATCH")
	}
	lease, err := service.control.RenewActionLease(store.ActionLeaseRenewal{ActionID: input.Fence.ActionId, Epoch: input.Fence.ExecutionEpoch,
		Owner: input.Owner, ClaimToken: input.ClaimToken, LeaseSeconds: int64(input.LeaseSeconds)})
	if err != nil {
		return nil, executionWriteError(err)
	}
	current, found, err := service.control.Get("action", input.Fence.ActionId)
	if err != nil || !found {
		return nil, status.Error(codes.DataLoss, "AGENT_EXECUTION_BINDING_INVALID")
	}
	claim, err := executionClaimProtocol(store.AdmissionResult{Lease: lease, Record: current}, service.control.Writer().LeaseUntil)
	if err != nil {
		return nil, err
	}
	return &agentv1.RenewAgentRunExecutionResponse{Claim: claim}, nil
}
