package api

import (
	"context"

	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (service *agentRunRecoveryService) PreviewProgress(_ context.Context, input *agentv1.PreviewAgentRecoveryProgressRequest) (*agentv1.PreviewAgentRecoveryProgressResponse, error) {
	current, err := service.authority()
	if err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.MetadataFence == nil || input.Request == nil || !agentproof.KnownRecoveryProgressMessage(input) {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	}
	if err := agentMetadataFence(input.MetadataFence, input.RunId, uint64(current.Epoch)); err != nil {
		return nil, err
	}
	result, err := service.run.control.PreviewAgentRecoveryProgress(store.AgentRecoveryProgressExpected{RunID: input.RunId, Fence: input.MetadataFence,
		ExpectedIndex: input.ExpectedIndex, RuntimeRevision: input.RuntimeRevision, Request: artifactMetadata(input.Request)})
	if err != nil {
		return nil, agentRecoveryRPCError(err)
	}
	return &agentv1.PreviewAgentRecoveryProgressResponse{Authoritative: true, Context: result}, nil
}

func (service *agentRunRecoveryService) CommitProgress(_ context.Context, input *agentv1.CommitAgentRecoveryProgressRequest) (*agentv1.CommitAgentRunRecoveryResponse, error) {
	current, err := service.authority()
	if err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || input.MetadataFence == nil || input.Request == nil || input.Receipt == nil || !agentproof.KnownRecoveryProgressMessage(input) {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	}
	if err := agentMetadataFence(input.MetadataFence, input.RunId, uint64(current.Epoch)); err != nil {
		return nil, err
	}
	result, err := service.run.control.CommitAgentRecoveryProgress(store.CommitAgentRecoveryProgressRequest{
		AgentRecoveryProgressExpected: store.AgentRecoveryProgressExpected{RunID: input.RunId, Fence: input.MetadataFence, ExpectedIndex: input.ExpectedIndex,
			RuntimeRevision: input.RuntimeRevision, Request: artifactMetadata(input.Request)}, OperationID: input.OperationId, Actor: input.Actor, Receipt: input.Receipt})
	if err != nil {
		return nil, agentRecoveryRPCError(err)
	}
	events := make([]*agentv1.AgentRunEventMetadata, 0, len(result.Events))
	for _, event := range result.Events {
		events = append(events, eventProtocol(event))
	}
	return &agentv1.CommitAgentRunRecoveryResponse{AlreadyApplied: result.Mutation.Status == store.OperatorMutationAlreadyApplied, Run: runProtocol(result.Run), Events: events}, nil
}
