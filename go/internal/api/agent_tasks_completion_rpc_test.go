package api

import (
	"context"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestTasksCompletionRegisteredRPCFinishesOnlyVerifiedPhaseAndReplays(t *testing.T) {
	control, nodes, task := agentTaskSettlementRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	if _, err := nodes.SettleTask(ctx, task); err != nil {
		t.Fatal(err)
	}
	input := &agentv1.CompleteAgentTasksRequest{SchemaVersion: 1, OperationId: "rpc-complete-tasks", Actor: task.Actor, Fence: task.Fence,
		RunId: task.RunId, Owner: task.Owner, ClaimToken: task.ClaimToken, ExpectedIndex: 5}
	result, err := nodes.CompleteTasks(ctx, input)
	if err != nil || result.AlreadyApplied || result.ActionState != "SUCCEEDED" || result.Run.Status != "running" || result.Run.NextIndex != 5 {
		t.Fatalf("registered task phase completion fabricated final output: %+v %v", result, err)
	}
	replay, err := nodes.CompleteTasks(ctx, input)
	if err != nil || !replay.AlreadyApplied || !proto.Equal(replay.Run, result.Run) {
		t.Fatalf("RPC replay duplicated completion: %v", err)
	}
	resources, err := control.GetResourceLeases(task.Fence.ActionId)
	if err != nil || len(resources) != 0 {
		t.Fatal("verified RPC completion retained phase resources")
	}
}

func TestTasksCompletionRPCRejectsAuthenticationUnknownFieldsAndUnresolvedTasks(t *testing.T) {
	control, nodes, task := agentTaskSettlementRPCFixture(t)
	input := &agentv1.CompleteAgentTasksRequest{SchemaVersion: 1, OperationId: "rpc-complete-unresolved", Actor: task.Actor, Fence: task.Fence,
		RunId: task.RunId, Owner: task.Owner, ClaimToken: task.ClaimToken, ExpectedIndex: 4}
	for _, bearer := range []string{"", "wrong"} {
		if _, err := nodes.CompleteTasks(controlRPCContext(t, bearer), input); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("unauthenticated phase completion: %v", err)
		}
	}
	ctx := controlRPCContext(t, testInternalBearer)
	for _, name := range []string{"schema", "fence", "unknown-input", "unknown-fence"} {
		t.Run(name, func(t *testing.T) {
			changed := proto.Clone(input).(*agentv1.CompleteAgentTasksRequest)
			switch name {
			case "schema":
				changed.SchemaVersion = 2
			case "fence":
				changed.Fence = nil
			case "unknown-input":
				changed.ProtoReflect().SetUnknown([]byte{0x80, 0x06, 0x01})
			case "unknown-fence":
				changed.Fence.ProtoReflect().SetUnknown([]byte{0x80, 0x06, 0x01})
			}
			if _, err := nodes.CompleteTasks(ctx, changed); status.Code(err) != codes.InvalidArgument {
				t.Fatalf("malformed completion accepted: %v", err)
			}
		})
	}
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := nodes.CompleteTasks(cancelled, input); status.Code(err) != codes.Canceled {
		t.Fatalf("cancelled completion: %v", err)
	}
	if _, err := nodes.CompleteTasks(ctx, input); err == nil {
		t.Fatal("unresolved task completed phase")
	}
	resources, err := control.GetResourceLeases(task.Fence.ActionId)
	if err != nil || len(resources) != 1 {
		t.Fatal("RPC refusal released unresolved phase")
	}
}
