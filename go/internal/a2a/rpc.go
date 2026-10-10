package a2a

import (
	"context"
	"errors"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type taskService struct {
	agentv1.UnimplementedA2ATaskControlServer
	store *Store
}

func rpcError(err error) error {
	switch {
	case err == nil:
		return nil
	case errors.Is(err, ErrTaskNotFound):
		return status.Error(codes.NotFound, "Task not found")
	case errors.Is(err, ErrInvalidTask):
		return status.Error(codes.InvalidArgument, "Invalid A2A task request")
	case errors.Is(err, ErrNotCancelable):
		return status.Error(codes.FailedPrecondition, "Task is already terminal")
	case errors.Is(err, ErrStaleExecution):
		return status.Error(codes.FailedPrecondition, "A2A execution authority is stale")
	default:
		return status.Error(codes.Unavailable, "A2A task store unavailable")
	}
}

func snapshot(task *Task, err error) (*agentv1.A2ATaskSnapshot, error) {
	if err != nil {
		return nil, rpcError(err)
	}
	document, err := task.PublicJSON()
	if err != nil {
		return nil, rpcError(err)
	}
	return &agentv1.A2ATaskSnapshot{TaskId: task.ID, State: task.State, Revision: task.Revision, PublicTaskJson: document,
		Fence: &commonv1.ActionFence{ActionId: task.ID, ExecutionEpoch: task.Epoch}}, nil
}

func (api *taskService) Submit(_ context.Context, in *agentv1.A2ASubmitRequest) (*agentv1.A2ATaskSnapshot, error) {
	if in.GetFence() == nil || in.Fence.ExecutionEpoch != 0 {
		return nil, rpcError(ErrInvalidTask)
	}
	return snapshot(api.store.Propose(in.Fence.ActionId, in.AgentId, in.ContextId, in.MessageJson))
}
func (api *taskService) Get(_ context.Context, in *agentv1.A2ATaskRef) (*agentv1.A2ATaskSnapshot, error) {
	return snapshot(api.store.Get(in.GetTaskId()))
}
func (api *taskService) Claim(_ context.Context, in *agentv1.A2ATaskRef) (*agentv1.A2ATaskSnapshot, error) {
	claim, err := api.store.Claim(in.GetTaskId())
	if err != nil {
		return nil, rpcError(err)
	}
	result, err := snapshot(claim.Task, nil)
	if err != nil {
		return nil, err
	}
	result.Execution = &agentv1.A2AExecutionRef{Fence: result.Fence, ExecutionToken: claim.Token}
	return result, nil
}
func (api *taskService) Renew(_ context.Context, in *agentv1.A2AExecutionRef) (*agentv1.A2ATaskSnapshot, error) {
	return snapshot(api.store.Renew(in.GetFence().GetActionId(), in.GetExecutionToken(), in.GetFence().GetExecutionEpoch()))
}
func (api *taskService) Finish(_ context.Context, in *agentv1.A2AFinishRequest) (*agentv1.A2ATaskSnapshot, error) {
	return snapshot(api.store.Finish(in.GetFence().GetActionId(), in.GetExecutionToken(), in.GetFence().GetExecutionEpoch(), in.GetContent(), in.GetFailure()))
}
func (api *taskService) Cancel(_ context.Context, in *agentv1.A2ATaskRef) (*agentv1.A2ATaskSnapshot, error) {
	return snapshot(api.store.Cancel(in.GetTaskId()))
}
func (api *taskService) List(_ context.Context, in *agentv1.A2AListFilter) (*agentv1.A2ATaskList, error) {
	tasks, err := api.store.List(int(in.GetLimit()))
	if err != nil {
		return nil, rpcError(err)
	}
	result := &agentv1.A2ATaskList{}
	for _, task := range tasks {
		item, err := snapshot(task, nil)
		if err != nil {
			return nil, err
		}
		result.Tasks = append(result.Tasks, item)
	}
	return result, nil
}
