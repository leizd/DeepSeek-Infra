package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

// 4e874ae1 added successor persistence with no tests. These cases pin the
// fail-closed boundaries: a malformed request, lost authority, a foreign run,
// an absent intent, and a reset that never authorized a generation must all
// refuse without writing.

func successorBindInput(t *testing.T, control *Control, input AgentNodeDispatchRequest) AgentNodeSuccessorBindRequest {
	t.Helper()
	return AgentNodeSuccessorBindRequest{
		OperationID: "node-coder-successor", Actor: input.Actor, ActionID: input.ActionID,
		Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID,
		Owner: input.Owner, ClaimToken: input.ClaimToken, ResetIndex: 3,
		Effect: "CONFIRMED", ResetAdmitted: true, ResetFenceMatches: true,
	}
}

func TestBindAgentNodeSuccessorRefusesAMalformedRequest(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	good := successorBindInput(t, control, input)
	for name, mutate := range map[string]func(*AgentNodeSuccessorBindRequest){
		"action-id":   func(in *AgentNodeSuccessorBindRequest) { in.ActionID = "" },
		"run-id":      func(in *AgentNodeSuccessorBindRequest) { in.RunID = "" },
		"zero-epoch":  func(in *AgentNodeSuccessorBindRequest) { in.Epoch = 0 },
		"node-id":     func(in *AgentNodeSuccessorBindRequest) { in.NodeID = "" },
		"long-node":   func(in *AgentNodeSuccessorBindRequest) { in.NodeID = strings.Repeat("a", 65) },
		"owner":       func(in *AgentNodeSuccessorBindRequest) { in.Owner = "   " },
		"claim-token": func(in *AgentNodeSuccessorBindRequest) { in.ClaimToken = "" },
		"reset-index": func(in *AgentNodeSuccessorBindRequest) { in.ResetIndex = -1 },
	} {
		t.Run(name, func(t *testing.T) {
			changed := good
			mutate(&changed)
			if _, err := control.BindAgentNodeSuccessor(changed); !errors.Is(err, ErrAgentNodeDispatchRequired) {
				t.Fatalf("%s was accepted: %v", name, err)
			}
		})
	}
}

func TestBindAgentNodeSuccessorRefusesWithoutIntentOrAuthority(t *testing.T) {
	// A dispatch that was never begun has no intent to attach a successor to.
	// The action is also not claimable, so the refusal comes before the intent
	// lookup; either way nothing is persisted.
	control, input := agentNodeDispatchFixture(t)
	missing := successorBindInput(t, control, input)
	missing.NodeID = "critic"
	if _, err := control.BindAgentNodeSuccessor(missing); err == nil {
		t.Fatal("absent intent was accepted")
	}

	// A different run cannot bind this action's successor.
	foreign := successorBindInput(t, control, input)
	foreign.RunID = "run_other"
	if _, err := control.BindAgentNodeSuccessor(foreign); err == nil {
		t.Fatal("foreign run was accepted")
	}

	// Lost action authority denies before any write and is never a generic error.
	lost, lostInput := agentNodeDispatchFixture(t)
	if _, err := lost.db.Exec("UPDATE control_cutover SET state=? WHERE domain=?", CutoverShadow, "action"); err != nil {
		t.Fatal(err)
	}
	if _, err := lost.BindAgentNodeSuccessor(successorBindInput(t, lost, lostInput)); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("lost authority still bound a successor: %v", err)
	}
}

func TestBindAgentNodeSuccessorRequiresASettledEffectAndAMatchingReset(t *testing.T) {
	for name, mutate := range map[string]func(*AgentNodeSuccessorBindRequest){
		"unknown-effect": func(in *AgentNodeSuccessorBindRequest) { in.Effect = "EFFECT_UNKNOWN" },
		"tool-wait":      func(in *AgentNodeSuccessorBindRequest) { in.Effect = "AWAITING_TOOLS" },
		"no-reset":       func(in *AgentNodeSuccessorBindRequest) { in.ResetAdmitted = false },
		"foreign-fence":  func(in *AgentNodeSuccessorBindRequest) { in.ResetFenceMatches = false },
	} {
		t.Run(name, func(t *testing.T) {
			control, input := agentNodeDispatchFixture(t)
			if _, err := control.BeginAgentNodeDispatch(input); err != nil {
				t.Fatal(err)
			}
			changed := successorBindInput(t, control, input)
			mutate(&changed)
			_, err := control.BindAgentNodeSuccessor(changed)
			var refusal *AgentSuccessorError
			if !errors.As(err, &refusal) || refusal.Code == "" {
				t.Fatalf("%s was accepted: %v", name, err)
			}
		})
	}
}

func TestAgentSuccessorErrorFallsBackToTheGenerationCode(t *testing.T) {
	// A nil receiver and an empty code both degrade to the generation refusal
	// instead of panicking or publishing an empty admission code.
	var missing *AgentSuccessorError
	if got := missing.Error(); got != "AGENT_RUNTIME_RESET_REQUIRES_GENERATION" {
		t.Fatalf("nil receiver returned %q", got)
	}
	if got := (&AgentSuccessorError{}).Error(); got != "AGENT_RUNTIME_RESET_REQUIRES_GENERATION" {
		t.Fatalf("empty code returned %q", got)
	}
	if got := (&AgentSuccessorError{Code: "AGENT_RUNTIME_SUCCESSOR_HELD"}).Error(); got != "AGENT_RUNTIME_SUCCESSOR_HELD" {
		t.Fatalf("typed code returned %q", got)
	}
}

func TestAdmitSuccessorDispatchRefusesAMalformedRequest(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	good := AgentSuccessorEpochRequest{OperationID: "node-coder-successor-run", Actor: input.Actor,
		ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID,
		Owner: input.Owner, ClaimToken: input.ClaimToken, Effect: "CONFIRMED",
		ResetAdmitted: true, ResetFenceMatches: true, ResetIndex: 3}
	for name, mutate := range map[string]func(*AgentSuccessorEpochRequest){
		"action-id":   func(in *AgentSuccessorEpochRequest) { in.ActionID = "" },
		"run-id":      func(in *AgentSuccessorEpochRequest) { in.RunID = "" },
		"zero-epoch":  func(in *AgentSuccessorEpochRequest) { in.Epoch = 0 },
		"node-id":     func(in *AgentSuccessorEpochRequest) { in.NodeID = "" },
		"owner":       func(in *AgentSuccessorEpochRequest) { in.Owner = "  " },
		"claim-token": func(in *AgentSuccessorEpochRequest) { in.ClaimToken = "" },
	} {
		t.Run(name, func(t *testing.T) {
			changed := good
			mutate(&changed)
			if _, err := control.AdmitSuccessorDispatch(changed); !errors.Is(err, ErrAgentNodeDispatchRequired) {
				t.Fatalf("%s was accepted: %v", name, err)
			}
		})
	}
}

func TestAdmitSuccessorDispatchHoldsUntilTakeoverIssuesTheEpoch(t *testing.T) {
	// On the current fence a successor is held; only a newer epoch issued by
	// claim or takeover may execute it.
	control, input := agentNodeDispatchFixture(t)
	if _, err := control.BeginAgentNodeDispatch(input); err != nil {
		t.Fatal(err)
	}
	epoch := AgentSuccessorEpochRequest{OperationID: "node-coder-successor-hold", Actor: input.Actor,
		ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID,
		Owner: input.Owner, ClaimToken: input.ClaimToken, Effect: "CONFIRMED",
		ResetAdmitted: true, ResetFenceMatches: true, ResetIndex: 3}
	_, err := control.AdmitSuccessorDispatch(epoch)
	var refusal *AgentSuccessorError
	if !errors.As(err, &refusal) {
		t.Fatalf("same-fence successor was admitted: %v", err)
	}
	// Any non-empty admission code is acceptable here; the invariant is that the
	// same fence never yields an admission, not which refusal wins first.
	if refusal.Code == "" {
		t.Fatal("refusal carried no admission code")
	}
}

func TestSuccessorOperationsRefuseInvalidCustodyWithoutWriting(t *testing.T) {
	control, input, _ := successorReadyFixture(t)
	bind := successorBindRequest(input)
	bound, err := control.BindAgentNodeSuccessor(bind)
	if err != nil {
		t.Fatal(err)
	}
	before, stored, found, err := control.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found {
		t.Fatalf("read original custody: %v", err)
	}
	count := countControlEvents(t, control)
	for _, tc := range []struct {
		name string
		edit func(*AgentNodeSuccessorBindRequest)
		want error
	}{
		{"missing operation", func(r *AgentNodeSuccessorBindRequest) { r.OperationID = "" }, ErrMutationRequestInvalid},
		{"foreign owner", func(r *AgentNodeSuccessorBindRequest) { r.Owner = "other-worker" }, ErrActionLeaseStale},
		{"foreign token", func(r *AgentNodeSuccessorBindRequest) { r.ClaimToken = "wrong-token" }, ErrInvalidClaimToken},
		{"foreign run", func(r *AgentNodeSuccessorBindRequest) { r.RunID = "run_other" }, ErrAgentRunExecutionRequired},
		{"absent node", func(r *AgentNodeSuccessorBindRequest) { r.NodeID = "critic" }, ErrAgentNodeDispatchRequired},
	} {
		t.Run(tc.name, func(t *testing.T) {
			changed := bind
			changed.OperationID = "bind-invalid-successor"
			tc.edit(&changed)
			if _, err := control.BindAgentNodeSuccessor(changed); !errors.Is(err, tc.want) {
				t.Fatalf("bind returned %v, want %v", err, tc.want)
			}
			admit := AgentSuccessorEpochRequest{
				OperationID: changed.OperationID, Actor: changed.Actor, ActionID: changed.ActionID,
				Epoch: changed.Epoch, RunID: changed.RunID, NodeID: changed.NodeID, Owner: changed.Owner,
				ClaimToken: changed.ClaimToken, Effect: changed.Effect, ResetAdmitted: changed.ResetAdmitted,
				ResetFenceMatches: changed.ResetFenceMatches, ResetIndex: changed.ResetIndex,
			}
			if _, err := control.AdmitSuccessorDispatch(admit); !errors.Is(err, tc.want) {
				t.Fatalf("admit returned %v, want %v", err, tc.want)
			}
		})
	}
	changed := bind
	changed.Actor = "operator:other-agent"
	if _, err := control.BindAgentNodeSuccessor(changed); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("rebound binding operation returned %v", err)
	}
	replay, err := control.BindAgentNodeSuccessor(bind)
	if err != nil || !replay.Replayed || replay.SuccessorOperationID != bound.SuccessorOperationID {
		t.Fatalf("original binding no longer replays: %+v %v", replay, err)
	}
	after, intent, found, err := control.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || after.Revision != before.Revision || !bytes.Equal(after.Payload, before.Payload) ||
		stored.OperationID != intent.OperationID || stored.SuccessorOperationID != intent.SuccessorOperationID ||
		countControlEvents(t, control) != count {
		t.Fatalf("refusal or replay changed the binding: %+v %v", after, err)
	}
}

func TestSuccessorHistoryCannotRewriteTheOriginalDispatch(t *testing.T) {
	control, input, begun := successorReadyFixture(t)
	if _, err := control.BindAgentNodeSuccessor(successorBindRequest(input)); err != nil {
		t.Fatal(err)
	}
	bound, intent, found, err := control.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || !nativeAgentSuccessorBindEdge(begun.Admission.Record, bound) {
		t.Fatalf("valid binding was not recognized: %+v %v", bound, err)
	}
	for _, tc := range []struct {
		name   string
		before Record
		edit   func(*AgentNodeDispatch)
	}{
		{"rewritten timestamp", begun.Admission.Record, func(node *AgentNodeDispatch) { node.RecordedAt++ }},
		{"rebased operation", begun.Admission.Record, func(node *AgentNodeDispatch) {
			node.Task += " changed"
			node.OperationID = agentNodeOperationID(*node)
			node.SuccessorOperationID = agent.SuccessorOperationID(node.OperationID, node.ActionID, node.ExecutionEpoch, node.NodeID, node.ResetIndex)
		}},
		{"replacement generation", bound, func(node *AgentNodeDispatch) {
			node.ResetIndex++
			node.SuccessorOperationID = agent.SuccessorOperationID(node.OperationID, node.ActionID, node.ExecutionEpoch, node.NodeID, node.ResetIndex)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			changed := intent
			tc.edit(&changed)
			var payload map[string]json.RawMessage
			if err := json.Unmarshal(bound.Payload, &payload); err != nil {
				t.Fatal(err)
			}
			dispatches, err := json.Marshal(map[string]AgentNodeDispatch{changed.NodeID: changed})
			if err != nil {
				t.Fatal(err)
			}
			payload[agentNodeDispatchField] = dispatches
			after := bound
			after.Revision = tc.before.Revision + 1
			after.Payload, err = json.Marshal(payload)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := readAgentNodeDispatches(after); err != nil {
				t.Fatalf("edited intent is invalid before checking the history edge: %v", err)
			}
			if nativeAgentSuccessorBindEdge(tc.before, after) {
				t.Fatal("changed history accepted as a successor binding")
			}
		})
	}
}
