package store

import (
	"errors"
	"fmt"
	"math"
	"reflect"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

func runtimeUpdateFixture(t *testing.T, reserve bool) (*Control, AgentRunEventMutation) {
	t.Helper()
	control := freshAgentEventControl(t)
	t.Cleanup(func() { _ = control.Close() })
	now := control.now()
	control.now = func() int64 { return now }
	if reserve {
		dual := dualEvaluate(t, control, "action")
		transition := CutoverTransition{Domain: "action", To: CutoverGoAuthoritative,
			ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
			TransferID: "runtime-update-action", Authority: emptyPythonInventoryCheckpoint(t)}
		var err error
		transition.Promotion, err = SignPromotionArtifact(PromotionArtifactForTransition(transition, dual, control.now(), "fleet-a", "production"), promotionTestPrivate)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.TransitionCutover(transition); err != nil {
			t.Fatal(err)
		}
	}
	events := []agent.RunEvent{{Type: "run_status", Status: "created"}, {Type: "run_status", Status: "planning"},
		{Type: "agent_plan", Plan: []agent.RunPlanNode{{ID: "coder", Task: "write"}}}, {Type: "run_status", Status: "awaiting_plan"}}
	for index, event := range events {
		input := eventMutation(t, control, int64(index), event)
		if index != 0 {
			input.Create = nil
		}
		if _, err := control.AppendAgentRunEvent(input); err != nil {
			t.Fatal(err)
		}
		if reserve && index == 1 {
			request := agentExecutionRequest(t, control)
			request.Phase, request.ExpectedIndex, request.LeaseSeconds = "plan", 2, 1
			if _, err := control.ClaimAgentRunExecution(request); err != nil {
				t.Fatal(err)
			}
		}
	}
	input := eventMutation(t, control, 4, agent.RunEvent{Type: "run_status", Status: "running"})
	input.Create = nil
	input.RuntimeUpdate = &AgentRunRuntimeUpdate{ExpectedRevision: 1,
		ExpectedRequest: agent.ArtifactRef{SHA256: strings.Repeat("b", 64), Length: 100},
		Request:         agent.ArtifactRef{SHA256: strings.Repeat("c", 64), Length: 120}}
	return control, input
}

func TestAgentRuntimeUpdateCommitsRequestRevisionAndConfirmationTogether(t *testing.T) {
	control, input := runtimeUpdateFixture(t, false)
	result, err := control.AppendAgentRunEvent(input)
	if err != nil {
		t.Fatal(err)
	}
	if result.Run.RuntimeRevision != 2 || result.Run.RuntimeVersion() != 2 || result.Run.Request != input.RuntimeUpdate.Request ||
		result.Run.Status != "running" || result.Run.NextIndex != 5 {
		t.Fatalf("runtime update was not part of confirmation: %+v", result.Run)
	}
	count := countControlEvents(t, control)
	replay, err := control.AppendAgentRunEvent(input)
	if err != nil || replay.Mutation.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(result.Run, replay.Run) || countControlEvents(t, control) != count {
		t.Fatalf("runtime replay changed revision/history: %+v %v", replay, err)
	}
	changed := input
	update := *input.RuntimeUpdate
	changed.RuntimeUpdate = &update
	update.Request.SHA256 = strings.Repeat("d", 64)
	if _, err := control.AppendAgentRunEvent(changed); !errors.Is(err, ErrMutationRequestReplayConflict) || countControlEvents(t, control) != count {
		t.Fatalf("operation identity was rebound to another request: %v", err)
	}
	options := OpenOptions{Path: control.path, Owner: "runtime-update-successor", Now: control.now,
		AuthorizeCutover: true, PromotionSignerPublicKey: control.promotionSignerKey, FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	after, found, err := reopened.AgentRunMetadata(input.RunID)
	if err != nil || !found || !reflect.DeepEqual(result.Run, after) {
		t.Fatalf("runtime revision did not survive reopen: %+v %v", after, err)
	}
}

func TestAgentRuntimeUpdateRejectsStaleOrMalformedBindingsWithoutWriting(t *testing.T) {
	control, original := runtimeUpdateFixture(t, false)
	before, _, err := control.AgentRunMetadata(original.RunID)
	if err != nil || before.RuntimeVersion() != 1 {
		t.Fatalf("legacy revision: %+v %v", before, err)
	}
	count := countControlEvents(t, control)
	for _, name := range []string{"zero-revision", "stale-revision", "old-request", "invalid-request", "empty-request", "cursor", "event", "cancel"} {
		t.Run(name, func(t *testing.T) {
			input := original
			update := *original.RuntimeUpdate
			input.RuntimeUpdate = &update
			input.OperationID = "runtime-refusal-" + name
			switch name {
			case "zero-revision":
				update.ExpectedRevision = 0
			case "stale-revision":
				update.ExpectedRevision = 2
			case "old-request":
				update.ExpectedRequest.SHA256 = strings.Repeat("d", 64)
			case "invalid-request":
				update.Request.SHA256 = "invalid"
			case "empty-request":
				update.Request.Length = 0
			case "cursor":
				input.ExpectedIndex--
			case "event":
				input.Event.Type = "agent_plan"
			case "cancel":
				input.Event.Status = "cancelled"
			}
			if _, err := control.AppendAgentRunEvent(input); err == nil {
				t.Fatal("invalid runtime swap accepted")
			}
			after, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil || !reflect.DeepEqual(before, after) || countControlEvents(t, control) != count {
				t.Fatalf("refused runtime swap wrote state: %+v %v", after, err)
			}
		})
	}
}

func TestAgentRuntimeUpdateRetainsActiveAndExpiredReservations(t *testing.T) {
	control, input := runtimeUpdateFixture(t, true)
	before := countControlEvents(t, control)
	for _, expired := range []bool{false, true} {
		if expired {
			now := control.now() + 2
			control.now = func() int64 { return now }
		}
		input.OperationID = fmt.Sprintf("runtime-reservation-%t", expired)
		if _, err := control.AppendAgentRunEvent(input); !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("reservation expiry=%t allowed a runtime swap: %v", expired, err)
		}
		current, _, err := control.AgentRunMetadata(input.RunID)
		if err != nil || current.RuntimeVersion() != 1 || current.Status != "awaiting_plan" || countControlEvents(t, control) != before {
			t.Fatalf("reserved runtime changed: %+v %v", current, err)
		}
	}
}

func TestAgentRuntimeUpdateRejectsRevisionOverflowBeforeAnyDatabaseAccess(t *testing.T) {
	run := agent.RunMetadata{Status: "awaiting_plan", RuntimeRevision: math.MaxInt64,
		Request: agent.ArtifactRef{SHA256: strings.Repeat("b", 64), Length: 100}, Plan: []agent.RunPlanNode{{ID: "coder", Task: "write"}}}
	input := AgentRunEventMutation{RuntimeUpdate: &AgentRunRuntimeUpdate{ExpectedRevision: math.MaxInt64, ExpectedRequest: run.Request,
		Request: agent.ArtifactRef{SHA256: strings.Repeat("c", 64), Length: 120}}, Event: agent.RunEvent{Type: "run_status", Status: "running"}}
	if err := applyAgentRuntimeUpdateTx(nil, &run, input); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("overflow: %v", err)
	}
}

func TestAgentRuntimeUpdateCannotAccompanyInitialCreation(t *testing.T) {
	control := freshAgentEventControl(t)
	defer control.Close()
	input := eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "created"})
	input.RuntimeUpdate = &AgentRunRuntimeUpdate{ExpectedRevision: 1, ExpectedRequest: input.Create.Request, Request: input.Create.Request}
	before := countControlEvents(t, control)
	if _, err := control.AppendAgentRunEvent(input); !errors.Is(err, ErrAgentRunMetadataInvalid) || countControlEvents(t, control) != before {
		t.Fatalf("creation accepted a runtime swap: %v", err)
	}
}

func TestAgentRuntimeUpdateRequiresQuiescentPlanConfirmation(t *testing.T) {
	for _, name := range []string{"created", "planning", "running", "cancelled", "done", "failed", "orphaned", "sticky-cancel", "empty-plan"} {
		t.Run(name, func(t *testing.T) {
			run := agent.RunMetadata{Status: name, Request: agent.ArtifactRef{SHA256: strings.Repeat("b", 64), Length: 100},
				Plan: []agent.RunPlanNode{{ID: "coder", Task: "write"}}}
			if name == "sticky-cancel" {
				run.Status, run.Cancelled = "awaiting_plan", true
			}
			if name == "empty-plan" {
				run.Status, run.Plan = "awaiting_plan", nil
			}
			input := AgentRunEventMutation{RuntimeUpdate: &AgentRunRuntimeUpdate{ExpectedRevision: 1, ExpectedRequest: run.Request,
				Request: agent.ArtifactRef{SHA256: strings.Repeat("c", 64), Length: 120}}, Event: agent.RunEvent{Type: "run_status", Status: "running"}}
			if err := applyAgentRuntimeUpdateTx(nil, &run, input); !errors.Is(err, ErrIllegalTransition) {
				t.Fatalf("state %s: %v", name, err)
			}
		})
	}
}

func TestAgentRuntimeUpdateConcurrentPreparationsCommitOneReference(t *testing.T) {
	control, input := runtimeUpdateFixture(t, false)
	before := countControlEvents(t, control)
	type outcome struct {
		result AgentRunEventResult
		err    error
	}
	results := make(chan outcome, 2)
	for _, id := range []string{"c", "d"} {
		go func(id string) {
			candidate := input
			update := *input.RuntimeUpdate
			candidate.RuntimeUpdate = &update
			candidate.OperationID = "runtime-concurrent-" + id
			update.Request.SHA256 = strings.Repeat(id, 64)
			result, err := control.AppendAgentRunEvent(candidate)
			results <- outcome{result, err}
		}(id)
	}
	var winner agent.ArtifactRef
	succeeded, conflicted := 0, 0
	for i := 0; i < 2; i++ {
		result := <-results
		if result.err == nil {
			succeeded++
			winner = result.result.Run.Request
		} else if errors.Is(result.err, ErrRevisionConflict) {
			conflicted++
		} else {
			t.Fatal(result.err)
		}
	}
	current, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil || succeeded != 1 || conflicted != 1 || current.RuntimeVersion() != 2 || current.Request != winner ||
		current.NextIndex != 5 || countControlEvents(t, control) != before+1 {
		t.Fatalf("concurrent preparations: %+v %v", current, err)
	}
}
