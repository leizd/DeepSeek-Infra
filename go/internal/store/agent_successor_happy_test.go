package store

import (
	"errors"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

// These cases drive the successor happy paths. Refusals were covered; the
// success branches that actually persist a minted generation and later admit it
// on a takeover epoch were not, and those are the lines that let a second
// provider call happen.

func successorReadyFixture(t *testing.T) (*Control, AgentNodeDispatchRequest, AgentNodeDispatchResult) {
	t.Helper()
	control, input := agentNodeDispatchFixture(t)
	begun, err := control.BeginAgentNodeDispatch(input)
	if err != nil {
		t.Fatalf("begin dispatch: %v", err)
	}
	return control, input, begun
}

func successorBindRequest(input AgentNodeDispatchRequest) AgentNodeSuccessorBindRequest {
	return AgentNodeSuccessorBindRequest{
		OperationID: "bind-coder-successor", Actor: input.Actor, ActionID: input.ActionID,
		Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID,
		Owner: input.Owner, ClaimToken: input.ClaimToken, ResetIndex: 5,
		Effect: "CONFIRMED", ResetAdmitted: true, ResetFenceMatches: true,
	}
}

func TestBindAgentNodeSuccessorPersistsTheMintedGeneration(t *testing.T) {
	control, input, begun := successorReadyFixture(t)
	intent := begun.Intent

	bound, err := control.BindAgentNodeSuccessor(successorBindRequest(input))
	if err != nil {
		t.Fatalf("a legal reset was refused: %v", err)
	}
	if bound.OperationID != intent.OperationID {
		t.Fatalf("the original operation id was replaced: %q", bound.OperationID)
	}
	if bound.ResetIndex != 5 {
		t.Fatalf("reset index is %d", bound.ResetIndex)
	}
	want := agent.SuccessorOperationID(intent.OperationID, intent.ActionID, intent.ExecutionEpoch, intent.NodeID, 5)
	if bound.SuccessorOperationID != want {
		t.Fatalf("minted %q, want %q", bound.SuccessorOperationID, want)
	}
	if bound.Replaced {
		t.Fatal("the binding reports a replacement")
	}

	// The stored intent keeps both ids, so a later read cannot lose the original.
	_, stored, found, err := control.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found {
		t.Fatalf("stored intent unreadable: %v", err)
	}
	if stored.OperationID != intent.OperationID || stored.SuccessorOperationID != want {
		t.Fatalf("stored intent lost a generation: %+v", stored)
	}

	// Replaying the identical bind is idempotent and reports a replay.
	replay := successorBindRequest(input)
	replay.OperationID = "bind-coder-successor-replay"
	again, err := control.BindAgentNodeSuccessor(replay)
	if err != nil {
		t.Fatalf("an identical successor was refused: %v", err)
	}
	if again.SuccessorOperationID != want {
		t.Fatalf("replay minted %q", again.SuccessorOperationID)
	}
}

func TestBindAgentNodeSuccessorRefusesASecondDifferentGeneration(t *testing.T) {
	control, input, _ := successorReadyFixture(t)
	if _, err := control.BindAgentNodeSuccessor(successorBindRequest(input)); err != nil {
		t.Fatal(err)
	}
	// A different reset index mints a different successor, which must not
	// silently overwrite the one already stored beside the original.
	second := successorBindRequest(input)
	second.OperationID = "bind-coder-successor-second"
	second.ResetIndex = 9
	_, err := control.BindAgentNodeSuccessor(second)
	var refused *AgentSuccessorError
	if !errors.As(err, &refused) {
		t.Fatalf("a second generation was accepted: %v", err)
	}
}

func TestBindingWithLostWriterFenceCanRetryWithoutLosingCustody(t *testing.T) {
	control, input, before := successorReadyFixture(t)
	originalClock := control.now
	start := originalClock()
	calls := 0
	control.now = func() int64 {
		calls++
		if calls == 1 {
			return start
		}
		return start - 1
	}
	bind := successorBindRequest(input)
	_, err := control.BindAgentNodeSuccessor(bind)
	control.now = originalClock
	if !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("writer fence loss returned %v", err)
	}
	record, stored, found, err := control.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || stored.OperationID != before.Intent.OperationID || stored.SuccessorOperationID != "" ||
		record.Revision != before.Admission.Record.Revision {
		t.Fatalf("failed binding changed the original intent: %+v %v", stored, err)
	}
	if _, applied, err := control.ReadOperatorMutation(bind.OperationID); err != nil || applied {
		t.Fatalf("failed binding left an applied receipt: %v %v", applied, err)
	}
	if bound, err := control.BindAgentNodeSuccessor(bind); err != nil || bound.Replayed || bound.SuccessorOperationID == "" {
		t.Fatalf("retry after rollback did not make the first binding: %+v %v", bound, err)
	}
}

func TestAdmitSuccessorDispatchHoldsOnTheOriginalFenceAndRequiresTakeover(t *testing.T) {
	control, input, begun := successorReadyFixture(t)
	intent := begun.Intent
	if _, err := control.BindAgentNodeSuccessor(successorBindRequest(input)); err != nil {
		t.Fatal(err)
	}

	epochRequest := AgentSuccessorEpochRequest{
		OperationID: "admit-coder-successor", Actor: input.Actor, ActionID: input.ActionID,
		Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID,
		Owner: input.Owner, ClaimToken: input.ClaimToken, Effect: "CONFIRMED",
		ResetAdmitted: true, ResetFenceMatches: true, ResetIndex: 5,
	}
	// The current fence cannot execute the stored generation by itself.
	_, err := control.AdmitSuccessorDispatch(epochRequest)
	var refusal *AgentSuccessorError
	if !errors.As(err, &refusal) {
		t.Fatalf("the original fence admitted a successor: %v", err)
	}
	if refusal.Code == "" {
		t.Fatal("the refusal carried no code")
	}
	// Nothing was released by the refusal.
	if _, stored, found, snapshotErr := control.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID); snapshotErr != nil || !found ||
		stored.OperationID != intent.OperationID {
		t.Fatalf("refusal changed the stored intent: %+v %v", stored, snapshotErr)
	}
}

func TestSuccessorDispatchPreservesOriginalCustodyAcrossTakeoverAndWriteFailures(t *testing.T) {
	control, input, begun := successorReadyFixture(t)
	bound, err := control.BindAgentNodeSuccessor(successorBindRequest(input))
	if err != nil {
		t.Fatal(err)
	}
	request := agentExecutionRequest(t, control)
	request.OperationID, request.Owner = "successor-takeover", "successor-worker"
	now := begun.Admission.Lease.LeaseUntil + 1
	options := OpenOptions{Path: control.path, Owner: "successor-control", Now: func() int64 { return now },
		AuthorizeCutover: true, PromotionSignerPublicKey: control.promotionSignerKey, FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = successor.Close() })
	taken, err := successor.ClaimAgentRunExecution(request)
	if err != nil || taken.Admission.Record.State != "RECONCILING" || taken.Admission.Lease.Epoch <= input.Epoch {
		t.Fatalf("takeover did not preserve unresolved custody: %+v %v", taken.Admission.Record, err)
	}
	lease := taken.Admission.Lease
	admit := AgentSuccessorEpochRequest{
		OperationID: "admit-takeover-successor", Actor: input.Actor, ActionID: input.ActionID,
		Epoch: lease.Epoch, RunID: input.RunID, NodeID: input.NodeID, Owner: lease.Owner,
		ClaimToken: lease.ClaimToken, Effect: "CONFIRMED", ResetAdmitted: true,
		ResetFenceMatches: true, ResetIndex: bound.ResetIndex,
	}
	input.Epoch, input.Owner, input.ClaimToken = lease.Epoch, lease.Owner, lease.ClaimToken
	input.OperationID = bound.SuccessorOperationID

	for _, step := range []struct {
		name  string
		state string
		run   func() error
	}{
		{"admission", "RECONCILING", func() error { _, err := successor.AdmitSuccessorDispatch(admit); return err }},
		{"dispatch", "CLAIMED", func() error { _, err := successor.BeginAgentNodeDispatch(input); return err }},
	} {
		t.Run(step.name, func(t *testing.T) {
			before, intent, found, err := successor.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
			if err != nil || !found || before.State != step.state {
				t.Fatalf("unexpected state before %s: %s, %v", step.name, before.State, err)
			}
			count := countControlEvents(t, successor)
			if _, err := successor.db.Exec(`CREATE TEMP TRIGGER reject_successor_event BEFORE INSERT ON control_events
				BEGIN SELECT RAISE(ABORT, 'injected successor event failure'); END`); err != nil {
				t.Fatal(err)
			}
			err = step.run()
			if _, dropErr := successor.db.Exec("DROP TRIGGER reject_successor_event"); dropErr != nil {
				t.Fatal(dropErr)
			}
			if err == nil || !strings.Contains(err.Error(), "injected successor event failure") {
				t.Fatalf("%s did not report the failing write: %v", step.name, err)
			}
			after, stored, found, err := successor.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
			if err != nil || !found || after.Revision != before.Revision || after.State != before.State ||
				stored.OperationID != intent.OperationID || stored.SuccessorOperationID != intent.SuccessorOperationID ||
				countControlEvents(t, successor) != count {
				t.Fatalf("failed %s changed durable custody: %+v %v", step.name, after, err)
			}
			if err := step.run(); err != nil {
				t.Fatalf("retry after rollback failed: %v", err)
			}
		})
		if t.Failed() {
			return
		}
	}

	record, stored, found, err := successor.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || record.State != "EXECUTING" || record.ExecutionEpoch != lease.Epoch ||
		stored.OperationID != begun.Intent.OperationID || stored.SuccessorOperationID != bound.SuccessorOperationID ||
		stored.ExecutionEpoch != begun.Intent.ExecutionEpoch {
		t.Fatalf("dispatch replaced the original intent or lost the takeover fence: %+v %+v %v", record, stored, err)
	}
	count := countControlEvents(t, successor)
	if replay, err := successor.AdmitSuccessorDispatch(admit); err != nil || replay.Record.Revision != record.Revision ||
		replay.Lease != lease || countControlEvents(t, successor) != count {
		t.Fatalf("admission replay modified custody: %+v %v", replay, err)
	}
}
