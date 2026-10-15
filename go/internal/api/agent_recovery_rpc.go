package api

import (
	"context"
	"errors"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type agentRunRecoveryService struct {
	agentv1.UnimplementedAgentRunRecoveryControlServer
	run *agentRunService
}

func (service *agentRunRecoveryService) authority() (store.CutoverRecord, error) {
	current, authoritative, err := service.run.authority()
	if err != nil {
		return current, err
	}
	if !authoritative {
		return current, status.Error(codes.PermissionDenied, "NATIVE_AGENT_RUN_NOT_AUTHORITATIVE")
	}
	return current, nil
}

// These request messages have only scalar leaves and explicit artifact/fence/
// selection children. Check every child before converting it to a Go store DTO;
// JSON detachment must never erase an unsupported wire option.
func agentRecoveryKnownProto(value proto.Message) bool {
	return value != nil && value.ProtoReflect().IsValid() && len(value.ProtoReflect().GetUnknown()) == 0
}

func (service *agentRunRecoveryService) Preview(_ context.Context, input *agentv1.PreviewAgentRunRecoveryRequest) (*agentv1.PreviewAgentRunRecoveryResponse, error) {
	current, err := service.authority()
	if err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || !agentRecoveryKnownProto(input) ||
		!agentRecoveryKnownProto(input.MetadataFence) || !agentRecoveryKnownProto(input.ExpectedRequest) || !agentRecoveryKnownProto(input.Selection) {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	}
	if err := agentMetadataFence(input.MetadataFence, input.RunId, uint64(current.Epoch)); err != nil {
		return nil, err
	}
	result, err := service.run.control.PreviewAgentRunRecovery(store.AgentRunRecoveryExpected{
		RunID: input.RunId, Fence: input.MetadataFence, ExpectedIndex: input.ExpectedIndex,
		ExpectedRuntimeRevision: input.ExpectedRuntimeRevision, ExpectedRequest: artifactMetadata(input.ExpectedRequest), Selection: input.Selection})
	if err != nil {
		return nil, agentRecoveryRPCError(err)
	}
	return &agentv1.PreviewAgentRunRecoveryResponse{Authoritative: true, Context: result.Context, ContextSha256: result.ContextSHA256}, nil
}

func (service *agentRunRecoveryService) Commit(_ context.Context, input *agentv1.CommitAgentRunRecoveryRequest) (*agentv1.CommitAgentRunRecoveryResponse, error) {
	current, err := service.authority()
	if err != nil {
		return nil, err
	}
	if input == nil || input.SchemaVersion != 1 || !agentRecoveryKnownProto(input) || !agentRecoveryKnownProto(input.Fence) ||
		!agentRecoveryKnownProto(input.ExpectedRequest) || !agentRecoveryKnownProto(input.Selection) ||
		!agentRecoveryKnownProto(input.Context) || !agentRecoveryKnownProto(input.Request) {
		return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	}
	if err := agentMetadataFence(input.Fence, input.RunId, uint64(current.Epoch)); err != nil {
		return nil, err
	}
	bodies := make([]agent.ArtifactRef, 0, len(input.EventBodies))
	for _, reference := range input.EventBodies {
		if !agentRecoveryKnownProto(reference) {
			return nil, status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
		}
		bodies = append(bodies, artifactMetadata(reference))
	}
	result, err := service.run.control.CommitAgentRunRecovery(store.CommitAgentRunRecoveryRequest{
		AgentRunRecoveryExpected: store.AgentRunRecoveryExpected{RunID: input.RunId, Fence: input.Fence, ExpectedIndex: input.ExpectedIndex,
			ExpectedRuntimeRevision: input.ExpectedRuntimeRevision, ExpectedRequest: artifactMetadata(input.ExpectedRequest), Selection: input.Selection},
		OperationID: input.OperationId, Actor: input.Actor, ContextSHA256: input.ContextSha256, Context: artifactMetadata(input.Context),
		Request: artifactMetadata(input.Request), EventBodies: bodies})
	if err != nil {
		return nil, agentRecoveryRPCError(err)
	}
	events := make([]*agentv1.AgentRunEventMetadata, 0, len(result.Events))
	for _, event := range result.Events {
		events = append(events, eventProtocol(event))
	}
	return &agentv1.CommitAgentRunRecoveryResponse{AlreadyApplied: result.Mutation.Status == store.OperatorMutationAlreadyApplied,
		Run: runProtocol(result.Run), Events: events}, nil
}

func agentRecoveryRPCError(err error) error {
	switch {
	case errors.Is(err, agent.ErrInvalidRunRecovery):
		return status.Error(codes.InvalidArgument, "AGENT_RUN_METADATA_INVALID")
	case errors.Is(err, store.ErrAgentRunRecoveryUnsettled), errors.Is(err, store.ErrAgentRunRecoveryRequired):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, store.ErrEmptyRecordID):
		return status.Error(codes.NotFound, "AGENT_RUN_NOT_FOUND")
	default:
		return agentMetadataWriteError(err)
	}
}
