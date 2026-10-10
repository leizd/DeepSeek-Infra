package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"
)

func TestAgentRunLifecyclePersistsApprovalRecoveryAndResume(t *testing.T) {
	control := openAuthority(t)
	t.Cleanup(func() {
		if control != nil {
			_ = control.Close()
		}
	})
	// Fresh isolated deployment only. Nonempty legacy Agent export/import is a
	// separate migration requirement, not proved by this lifecycle test.
	checkpoint := emptyPythonInventoryCheckpoint(t)
	if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "agent_run")
	request := CutoverTransition{Domain: "agent_run", To: CutoverGoAuthoritative, ExpectedRevision: dual.Revision,
		ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken, TransferID: "fresh-agent-deployment", Authority: checkpoint}
	artifact := PromotionArtifactForTransition(request, dual, control.now(), "fleet-a", "production")
	var err error
	request.Promotion, err = SignPromotionArtifact(artifact, promotionTestPrivate)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(request); err != nil {
		t.Fatal(err)
	}
	cutover, err := control.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	states := []string{"created", "planning", "awaiting_plan", "awaiting_plan", "running", "running",
		"orphaned", "running", "failed", "running", "cancelled", "running", "done", "running", "done"}
	for index, state := range states {
		payload, _ := json.Marshal(map[string]any{"runId": "run_lifecycle", "nextIndex": index, "inputArtifactId": "input-fixture"})
		mutation := OperatorMutation{OperationID: fmt.Sprintf("agent-lifecycle-%d", index), ActionID: "run_lifecycle",
			Actor: "operator:qualification", Record: Record{Domain: "agent_run", ID: "run_lifecycle",
				Revision: int64(index + 1), State: state, Payload: payload}}
		result, err := control.ApplyOperatorMutation(mutation)
		if err != nil || result.ExecutionEpoch != uint64(cutover.Epoch) {
			t.Fatalf("%s transition: %+v %v", state, result, err)
		}
		count := countControlEvents(t, control)
		replay, err := control.ApplyOperatorMutation(mutation)
		if err != nil || replay.Status != OperatorMutationAlreadyApplied || countControlEvents(t, control) != count {
			t.Fatalf("%s retry changed history: %+v %v", state, replay, err)
		}
		// A metadata transition neither claims an action nor advances its epoch.
		if _, exists, err := control.GetActionLease(mutation.ActionID); err != nil || exists {
			t.Fatalf("metadata granted execution authority: %v %v", exists, err)
		}
		if state == "awaiting_plan" || state == "orphaned" {
			options := OpenOptions{Path: control.path, Owner: fmt.Sprintf("agent-successor-%d", index), Now: control.now,
				AuthorizeCutover: true, PromotionSignerPublicKey: control.promotionSignerKey,
				FleetID: control.fleetID, Environment: control.environment}
			if err := control.Close(); err != nil {
				t.Fatal(err)
			}
			control, err = OpenControl(options)
			if err != nil {
				t.Fatal(err)
			}
		}
		record, found, err := control.Get("agent_run", "run_lifecycle")
		if err != nil || !found || record.State != state || record.Revision != int64(index+1) {
			t.Fatalf("persisted %s: %+v %v %v", state, record, found, err)
		}
	}
}

func TestAgentRunLifecycleKeepsAdmissionAndIllegalTransitions(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	mutation := OperatorMutation{OperationID: "unpromoted-agent-create", ActionID: "run_denied", Actor: "operator:test",
		Record: Record{Domain: "agent_run", ID: "run_denied", Revision: 1, State: "created", Payload: json.RawMessage(`{"runId":"run_denied"}`)}}
	if _, err := control.ApplyOperatorMutation(mutation); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("unpromoted agent domain wrote: %v", err)
	}
	for _, transition := range [][2]string{{"", "running"}, {"created", "done"}, {"awaiting_plan", "orphaned"},
		{"done", "created"}, {"orphaned", "planning"}, {"running", "awaiting_plan"}, {"unknown", "running"}} {
		if LegalTransition("agent_run", transition[0], transition[1]) {
			t.Fatalf("illegal transition accepted: %v", transition)
		}
	}
	for _, state := range []string{"created", "planning", "running"} {
		if !LegalTransition("agent_run", state, "orphaned") {
			t.Fatalf("startup cannot checkpoint interrupted %s", state)
		}
	}
}
