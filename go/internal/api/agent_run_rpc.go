package api

import (
	"context"
	"errors"
	"sort"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type agentRunService struct {
	agentv1.UnimplementedAgentRunControlServer
	control *store.Control
	health  ControlPlaneHealth
}

func (service *agentRunService) processAuthoritative() bool {
	return service.health.Mode == controlv1.RuntimeMode_RUNTIME_MODE_AUTHORITATIVE && service.health.MutationAuthority == "go"
}

func (service *agentRunService) authority() (store.CutoverRecord, bool, error) {
	if service.control == nil {
		return store.CutoverRecord{}, false, status.Error(codes.Unavailable, "CONTROL_STORE_UNAVAILABLE")
	}
	current, err := service.control.GetCutover("agent_run")
	if err != nil {
		return current, false, status.Error(codes.DataLoss, "AGENT_CONTROL_AUTHORITY_READ_FAILED")
	}
	return current, service.processAuthoritative() && store.IsDomainGoAuthoritative(current.State), nil
}

func (service *agentRunService) GetAuthority(context.Context, *agentv1.AgentRunAuthorityInput) (*agentv1.AgentRunAuthorityOutput, error) {
	current, authoritative, err := service.authority()
	if err != nil {
		return nil, err
	}
	return &agentv1.AgentRunAuthorityOutput{Authoritative: authoritative, MetadataEpoch: uint64(current.Epoch)}, nil
}

func agentMetadataFence(fence *commonv1.ActionFence, id string, epoch uint64) error {
	if fence == nil || fence.ActionId != id {
		return status.Error(codes.FailedPrecondition, "FENCE_MISMATCH")
	}
	if err := internalprotocol.AdmitCommand(fence, epoch); err != nil {
		return status.Error(codes.FailedPrecondition, err.Error())
	}
	return nil
}

func artifactMetadata(input *agentv1.AgentArtifactReference) agent.ArtifactRef {
	return agent.ArtifactRef{SHA256: input.GetSha256(), Length: input.GetLength()}
}

func eventMetadata(input *agentv1.AgentRunEventMetadata) agent.RunEvent {
	event := agent.RunEvent{Type: input.Type, Phase: input.Phase, Status: input.Status, Scope: input.Scope, Failed: input.Failed,
		DurationMS: input.DurationMs, PromptTokens: input.PromptTokens, CompletionTokens: input.CompletionTokens, Body: artifactMetadata(input.Body)}
	if input.HasPlan {
		event.Plan = make([]agent.RunPlanNode, 0, len(input.Plan))
		for _, node := range input.Plan {
			event.Plan = append(event.Plan, agent.RunPlanNode{ID: node.GetId(), Task: node.GetTask(), DependsOn: node.GetDependsOn()})
		}
	}
	return event
}

func (service *agentRunService) AppendEvent(_ context.Context, input *agentv1.AppendAgentRunEventRequest) (*agentv1.AppendAgentRunEventResponse, error) {
	if !service.processAuthoritative() {
		return nil, status.Error(codes.PermissionDenied, "MUTATION_DENIED")
	}
	if service.control == nil {
		return nil, status.Error(codes.Unavailable, "CONTROL_STORE_UNAVAILABLE")
	}
	if input == nil || input.SchemaVersion != 1 || input.Event == nil || !input.Event.HasPlan && len(input.Event.Plan) != 0 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	}
	// Fence equality is checked again against the live epoch inside the store's
	// authority/write transaction, so this RPC cannot promote a caller's epoch.
	if input.Fence == nil || input.Fence.ActionId != input.RunId {
		return nil, status.Error(codes.FailedPrecondition, "FENCE_MISMATCH")
	}
	mutation := store.AgentRunEventMutation{OperationID: input.OperationId, Actor: input.Actor, RunID: input.RunId,
		ExpectedIndex: input.ExpectedIndex, Fence: input.Fence, Event: eventMetadata(input.Event)}
	if input.Create != nil {
		mutation.Create = &agent.RunMetadata{Preset: input.Create.Preset, ConfirmPlan: input.Create.ConfirmPlan,
			ConversationID: input.Create.ConversationId, MessageID: input.Create.MessageId, Request: artifactMetadata(input.Create.Request)}
	}
	result, err := service.control.AppendAgentRunEvent(mutation)
	if err != nil {
		return nil, agentMetadataWriteError(err)
	}
	return &agentv1.AppendAgentRunEventResponse{AlreadyApplied: result.Mutation.Status == store.OperatorMutationAlreadyApplied,
		Event: eventProtocol(result.Event), Run: runProtocol(result.Run)}, nil
}

func agentMetadataWriteError(err error) error {
	switch {
	case errors.Is(err, store.ErrRevisionConflict), errors.Is(err, store.ErrMutationRequestReplayConflict):
		return status.Error(codes.Aborted, err.Error())
	case errors.Is(err, store.ErrAgentRunMetadataInvalid), errors.Is(err, store.ErrMutationRequestInvalid),
		errors.Is(err, store.ErrInvalidPayload), errors.Is(err, store.ErrSecretDetected):
		return status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	case errors.Is(err, internalprotocol.ErrEmptyActionID), errors.Is(err, internalprotocol.ErrZeroEpoch),
		errors.Is(err, internalprotocol.ErrFenceMismatch), errors.Is(err, internalprotocol.ErrStaleEpoch),
		errors.Is(err, store.ErrCutoverNotAuthorized), errors.Is(err, store.ErrWriterFenceHeld),
		errors.Is(err, store.ErrSchemaInactive), errors.Is(err, store.ErrIllegalTransition):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, store.ErrCorruptRecord):
		return status.Error(codes.DataLoss, "AGENT_CONTROL_HISTORY_INVALID")
	default:
		return status.Error(codes.Unavailable, "AGENT_CONTROL_WRITE_FAILED")
	}
}

func (service *agentRunService) GetRun(_ context.Context, input *agentv1.GetAgentRunRequest) (*agentv1.GetAgentRunResponse, error) {
	if input == nil || !store.ValidRecordID(input.RunId) {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	}
	current, authoritative, err := service.authority()
	if err != nil {
		return nil, err
	}
	if err := agentMetadataFence(input.Fence, input.RunId, uint64(current.Epoch)); err != nil {
		return nil, err
	}
	run, found, err := service.control.AgentRunMetadata(input.RunId)
	if err != nil {
		return nil, status.Error(codes.DataLoss, "AGENT_CONTROL_HISTORY_INVALID")
	}
	if !found {
		return nil, status.Error(codes.NotFound, "AGENT_RUN_NOT_FOUND")
	}
	return &agentv1.GetAgentRunResponse{Authoritative: authoritative, Run: runProtocol(run)}, nil
}

func (service *agentRunService) EventsAfter(_ context.Context, input *agentv1.AgentRunEventsRequest) (*agentv1.AgentRunEventsResponse, error) {
	if input == nil || !store.ValidRecordID(input.RunId) || input.After < -1 || input.Limit == 0 || input.Limit > 1024 {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	}
	current, authoritative, err := service.authority()
	if err != nil {
		return nil, err
	}
	if err := agentMetadataFence(input.Fence, input.RunId, uint64(current.Epoch)); err != nil {
		return nil, err
	}
	events, err := service.control.AgentRunEventsAfter(input.RunId, input.After, int(input.Limit))
	if errors.Is(err, store.ErrEmptyRecordID) {
		return nil, status.Error(codes.NotFound, "AGENT_RUN_NOT_FOUND")
	}
	if err != nil {
		return nil, status.Error(codes.DataLoss, "AGENT_CONTROL_HISTORY_INVALID")
	}
	output := &agentv1.AgentRunEventsResponse{Authoritative: authoritative, Events: make([]*agentv1.AgentRunEventMetadata, 0, len(events))}
	for _, event := range events {
		output.Events = append(output.Events, eventProtocol(event))
	}
	return output, nil
}

func artifactProtocol(ref agent.ArtifactRef) *agentv1.AgentArtifactReference {
	return &agentv1.AgentArtifactReference{Sha256: ref.SHA256, Length: ref.Length}
}

func planProtocol(plan []agent.RunPlanNode) []*agentv1.AgentRunPlanNode {
	result := make([]*agentv1.AgentRunPlanNode, 0, len(plan))
	for _, node := range plan {
		result = append(result, &agentv1.AgentRunPlanNode{Id: node.ID, Task: node.Task, DependsOn: node.DependsOn})
	}
	return result
}

func eventProtocol(event agent.RunEvent) *agentv1.AgentRunEventMetadata {
	return &agentv1.AgentRunEventMetadata{Fence: &commonv1.ActionFence{ActionId: event.ActionID, ExecutionEpoch: event.ExecutionEpoch},
		RunId: event.RunID, Index: event.Index, CreatedAt: event.CreatedAt, Type: event.Type, Phase: event.Phase, Status: event.Status, Scope: event.Scope,
		Plan: planProtocol(event.Plan), HasPlan: event.Type == "agent_plan", Failed: event.Failed, DurationMs: event.DurationMS,
		PromptTokens: event.PromptTokens, CompletionTokens: event.CompletionTokens, Body: artifactProtocol(event.Body)}
}

func runProtocol(run agent.RunMetadata) *agentv1.AgentRunSnapshot {
	output := &agentv1.AgentRunSnapshot{RunId: run.RunID, Status: run.Status, NextIndex: run.NextIndex, Preset: run.Preset,
		ConfirmPlan: run.ConfirmPlan, ConversationId: run.ConversationID, MessageId: run.MessageID, Request: artifactProtocol(run.Request),
		Plan: planProtocol(run.Plan), FinalAfter: run.FinalAfter, LastEvent: eventProtocol(run.LastEvent)}
	nodes := run.Nodes()
	ids := make([]string, 0, len(nodes))
	for id := range nodes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		node := nodes[id]
		output.Nodes = append(output.Nodes, &agentv1.AgentRunNodeMetadata{Id: id, State: node.State, Attempts: node.Attempts, LatencyMs: node.LatencyMS,
			PromptTokens: node.PromptTokens, CompletionTokens: node.CompletionTokens, Failed: node.Failed})
	}
	return output
}
