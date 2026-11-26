package store

import (
	"errors"
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
