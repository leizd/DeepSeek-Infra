package store

import (
	"errors"
	"reflect"
	"testing"
)

func synthesisRequest(t *testing.T, control *Control) AgentRunExecutionRequest {
	t.Helper()
	request := agentExecutionRequest(t, control)
	request.Phase, request.OperationID, request.ExpectedIndex = "synthesis", "claim-synthesis", 6
	return request
}

func TestSynthesisRequiresVerifiedTerminalTaskPhaseAndPreservesReplay(t *testing.T) {
	control, complete := tasksCompletionFixture(t, false)
	request := synthesisRequest(t, control)
	before, err := control.ExportSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, ErrAgentNodeDependencyUnverified) {
		t.Fatalf("unsettled phase admitted synthesis: %v", err)
	}
	after, err := control.ExportSnapshot()
	if err != nil || after.Digest != before.Digest {
		t.Fatalf("denial mutated control state: %v", err)
	}
	tasks, err := control.CompleteAgentTasks(complete)
	if err != nil {
		t.Fatal(err)
	}
	first, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	binding, err := decodeAgentExecution(first.Admission.Record)
	if err != nil || binding.Phase != "synthesis" || first.Admission.Record.ID == tasks.Action.ID {
		t.Fatal("synthesis reused the task fence", err)
	}
	replay, err := control.ClaimAgentRunExecution(request)
	if err != nil || replay.Mutation.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(first.Admission, replay.Admission) {
		t.Fatalf("synthesis replay changed admission: %v", err)
	}
	lease := first.Admission.Lease
	dispatch := AgentNodeDispatchRequest{OperationID: "begin-synthesis", Actor: "rust-agent-worker", ActionID: lease.ActionID,
		Epoch: lease.Epoch, RunID: request.RunID, NodeID: "synthesizer", Owner: lease.Owner, ClaimToken: lease.ClaimToken}
	reserved, err := control.BeginAgentNodeDispatch(dispatch)
	if err != nil || !reserved.Fresh || reserved.Intent.Phase != "synthesis" || reserved.Intent.Task != "synthesize verified agent outputs" ||
		!reflect.DeepEqual(reserved.Intent.DependsOn, []string{"coder", "critic"}) {
		t.Fatalf("synthesis did not derive its authoritative inputs: %+v %v", reserved, err)
	}
	again, err := control.BeginAgentNodeDispatch(dispatch)
	if err != nil || again.Fresh || !reflect.DeepEqual(again.Intent, reserved.Intent) {
		t.Fatalf("synthesis redispatch admitted: %v", err)
	}
	dispatch.OperationID = "reject-coder"
	dispatch.NodeID = "coder"
	if _, err := control.BeginAgentNodeDispatch(dispatch); !errors.Is(err, ErrAgentNodeDispatchRequired) {
		t.Fatalf("task node entered synthesis phase: %v", err)
	}
}

func TestSynthesisDeniesForgedPublicProjectionAndCorruptOriginalCompletion(t *testing.T) {
	control := agentExecutionControl(t, true)
	request := agentExecutionRequest(t, control)
	request.Phase = "synthesis"
	if _, err := control.ClaimAgentRunExecution(request); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("unfinished tasks admitted synthesis: %v", err)
	}
	control, complete := tasksCompletionFixture(t, false)
	terminal, err := control.CompleteAgentTasks(complete)
	if err != nil {
		t.Fatal(err)
	}
	restore := taskBoundaryHistoryView(t, control, "action", terminal.Action.ID, terminal.Action.Revision, nil)
	defer restore()
	if _, err := control.ClaimAgentRunExecution(synthesisRequest(t, control)); err == nil {
		t.Fatal("missing immutable task completion admitted synthesis")
	}
}

func TestSynthesisSourceLookupFailsClosedOnStorageErrorsAndWrongBinding(t *testing.T) {
	control, complete := tasksCompletionFixture(t, false)
	if _, err := control.CompleteAgentTasks(complete); err != nil {
		t.Fatal(err)
	}
	request := synthesisRequest(t, control)
	run, found, err := control.AgentRunMetadata(request.RunID)
	if err != nil || !found {
		t.Fatal("fixture run missing", err)
	}
	scope := AgentRunExecutionBinding{SchemaVersion: 1, RunID: run.RunID, Phase: "synthesis", MetadataIndex: run.NextIndex,
		MetadataEpoch: request.Fence.ExecutionEpoch, Request: run.Request, PlanDigest: executionPlanDigest(run)}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := control.verifiedAgentSynthesisSourceTx(tx, scope); err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*AgentRunExecutionBinding){
		func(b *AgentRunExecutionBinding) { b.Phase = "tasks" }, func(b *AgentRunExecutionBinding) { b.MetadataIndex++ },
		func(b *AgentRunExecutionBinding) { b.MetadataEpoch++ }, func(b *AgentRunExecutionBinding) { b.PlanDigest = "different" },
		func(b *AgentRunExecutionBinding) { b.Request.Length++ }, func(b *AgentRunExecutionBinding) { b.RunID = "missing" },
	} {
		wrong := scope
		change(&wrong)
		if _, err := control.verifiedAgentSynthesisSourceTx(tx, wrong); err == nil {
			t.Fatal("rebound source accepted")
		}
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.verifiedAgentSynthesisSourceTx(tx, scope); err == nil {
		t.Fatal("closed transaction supplied synthesis evidence")
	}
}
