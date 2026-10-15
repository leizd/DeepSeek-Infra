package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func recoveryAdmissionFixture(t *testing.T, status string) (*Control, CommitAgentRunRecoveryRequest) {
	t.Helper()
	control, confirmation := runtimeUpdateFixture(t, false)
	confirmed, err := control.AppendAgentRunEvent(confirmation)
	if err != nil {
		t.Fatal(err)
	}
	stop := eventMutation(t, control, confirmed.Run.NextIndex, agent.RunEvent{Type: "run_status", Status: status})
	stop.Create = nil
	stopped, err := control.AppendAgentRunEvent(stop)
	if err != nil {
		t.Fatal(err)
	}
	expected := AgentRunRecoveryExpected{RunID: stop.RunID, Fence: stop.Fence, ExpectedIndex: stopped.Run.NextIndex,
		ExpectedRuntimeRevision: stopped.Run.RuntimeVersion(), ExpectedRequest: stopped.Run.Request,
		Selection: &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME}}
	preview, err := control.PreviewAgentRunRecovery(expected)
	if err != nil {
		t.Fatal(err)
	}
	input := CommitAgentRunRecoveryRequest{AgentRunRecoveryExpected: expected, OperationID: "recover-first-generation", Actor: "operator:recovery-test",
		ContextSHA256: preview.ContextSHA256, Context: agent.ArtifactRef{SHA256: strings.Repeat("e", 64), Length: 512},
		Request: agent.ArtifactRef{SHA256: strings.Repeat("d", 64), Length: 130}}
	for index := range agentRecoveryEvents(preview) {
		input.EventBodies = append(input.EventBodies, agent.ArtifactRef{SHA256: strings.Repeat(fmt.Sprintf("%x", index+1), 64), Length: 40})
	}
	return control, input
}

func TestRecoveryAdmissionCommitsGenerationRequestAndEventsWithoutEffectAuthority(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	before, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found || !before.CancellationActive() {
		t.Fatal(err)
	}
	count := countControlEvents(t, control)
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	if result.Run.SchemaVersion != 3 || result.Run.RuntimeVersion() != before.RuntimeVersion()+1 ||
		result.Run.Request != input.Request || result.Run.Recovery == nil || result.Run.Recovery.Generation != 1 ||
		result.Run.Recovery.StartIndex != before.NextIndex || !result.Run.Cancelled || result.Run.CancellationActive() ||
		result.Run.Status != "running" || result.Run.NextIndex != before.NextIndex+3 ||
		result.Run.Nodes()["coder"].State != "retrying" || result.Run.FinalAfter != before.NextIndex+2 ||
		countControlEvents(t, control) != count+3 {
		t.Fatal("recovery admission was partial or erased historical cancellation", result.Run)
	}
	if len(result.Events) != 3 || result.Events[0].Type != "run_status" || result.Events[1].Type != "agent_reset" ||
		result.Events[1].Phase != "coder" || result.Events[2].Type != "final_reset" {
		t.Fatal("resume event order differs from retained behavior", result.Events)
	}
	if len(result.Run.Plan) != len(before.Plan) || !reflect.DeepEqual(result.Run.Plan, before.Plan) {
		t.Fatal("effective recovery plan replaced the retained public plan")
	}
	var actions, resources int
	if control.db.QueryRow("SELECT count(*) FROM action_journal").Scan(&actions) != nil || actions != 0 ||
		control.db.QueryRow("SELECT count(*) FROM action_resource_leases").Scan(&resources) != nil || resources != 0 {
		t.Fatal("metadata admission granted a worker effect or changed resources")
	}
	encoded, err := json.Marshal(result.Run)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := decodeAgentMetadata(Record{Domain: "agent_run", ID: input.RunID, Revision: result.Run.NextIndex,
		State: result.Run.Status, ExecutionEpoch: result.Run.LastEvent.ExecutionEpoch, Payload: encoded}); err != nil {
		t.Fatal("new scope is unreadable", err)
	}
}

func TestRecoveryPreviewIsDetachedAndDoesNotWriteOrChooseAnotherTask(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "orphaned")
	count := countControlEvents(t, control)
	preview, err := control.PreviewAgentRunRecovery(input.AgentRunRecoveryExpected)
	if err != nil || preview.Context.Generation != 1 || len(preview.Context.Execute) != 1 || preview.Context.Execute[0].Task != "write" {
		t.Fatal("preview changed the retained decision", preview, err)
	}
	preview.Context.Execute[0].Task = "injected caller task"
	preview.Context.Plan[0].Task = "another injected task"
	if countControlEvents(t, control) != count {
		t.Fatal("preview persisted caller-owned state")
	}
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil || result.Run.Recovery.Metadata.Execute[0].Task != "write" || result.Run.Recovery.Metadata.Plan[0].Task != "write" {
		t.Fatal("caller rebound an audited task through a mutable preview", err)
	}
}

func TestRecoveryRejectsUnknownSelectionBeforeJSONDetachment(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	input.Selection = proto.Clone(input.Selection).(*agentv1.AgentRunRecoverySelection)
	input.Selection.ProtoReflect().SetUnknown([]byte{0x80, 0x06, 0x01})
	count := countControlEvents(t, control)
	if _, err := control.PreviewAgentRunRecovery(input.AgentRunRecoveryExpected); !errors.Is(err, agent.ErrInvalidRunRecovery) {
		t.Fatal("JSON detachment silently discarded an unsupported recovery option", err)
	}
	if _, err := control.CommitAgentRunRecovery(input); !errors.Is(err, agent.ErrInvalidRunRecovery) || countControlEvents(t, control) != count {
		t.Fatal("unknown options were erased before authority admission", err)
	}
}

func TestRecoveryOperationReplaysOriginalHeadersAfterLaterPublicEvents(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	first, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	note := eventMutation(t, control, first.Run.NextIndex, agent.RunEvent{Type: "agent_note", Phase: "coder"})
	note.Create = nil
	advanced, err := control.AppendAgentRunEvent(note)
	if err != nil {
		t.Fatal(err)
	}
	count := countControlEvents(t, control)
	replayed, err := control.CommitAgentRunRecovery(input)
	if err != nil || replayed.Mutation.Status != OperatorMutationAlreadyApplied ||
		!reflect.DeepEqual(first.Events, replayed.Events) || !reflect.DeepEqual(advanced.Run, replayed.Run) || countControlEvents(t, control) != count {
		t.Fatal("recovery replay changed scope/cursor or returned unrelated events", replayed, err)
	}
	changed := input
	changed.Request.SHA256 = strings.Repeat("f", 64)
	if _, err := control.CommitAgentRunRecovery(changed); !errors.Is(err, ErrMutationRequestReplayConflict) || countControlEvents(t, control) != count {
		t.Fatal("operation identity was rebound to another immutable request", err)
	}
}

func TestRecoveryCommitRejectsStaleForgedOrMalformedInputsWithoutWriting(t *testing.T) {
	for _, name := range []string{"cursor", "runtime", "old-request", "context-hash", "no-request-swap", "body-count", "invalid-body", "fence", "authority"} {
		t.Run(name, func(t *testing.T) {
			control, original := recoveryAdmissionFixture(t, "failed")
			encoded, _ := json.Marshal(original)
			var input CommitAgentRunRecoveryRequest
			if json.Unmarshal(encoded, &input) != nil {
				t.Fatal("cannot detach fixture")
			}
			before, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil {
				t.Fatal(err)
			}
			count := countControlEvents(t, control)
			switch name {
			case "cursor":
				input.ExpectedIndex--
			case "runtime":
				input.ExpectedRuntimeRevision++
			case "old-request":
				input.ExpectedRequest.SHA256 = strings.Repeat("9", 64)
			case "context-hash":
				input.ContextSHA256 = strings.Repeat("0", 64)
			case "no-request-swap":
				input.Request = input.ExpectedRequest
			case "body-count":
				input.EventBodies = input.EventBodies[:1]
			case "invalid-body":
				input.EventBodies[0].Length = 0
			case "fence":
				input.Fence.ActionId = "another-run"
			case "authority":
				control.authorizeCutover = false
			}
			if _, err := control.CommitAgentRunRecovery(input); err == nil {
				t.Fatal("invalid recovery was accepted")
			}
			after, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil || !reflect.DeepEqual(before, after) || countControlEvents(t, control) != count {
				t.Fatal("refusal changed durable state", err)
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatal("refusal retained an operation receipt", err)
			}
		})
	}
}

func TestConcurrentRecoveryCommitsUseOneCursorAndOneGeneration(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "orphaned")
	count := countControlEvents(t, control)
	errorsSeen := make(chan error, 2)
	var workers sync.WaitGroup
	for index := range 2 {
		workers.Add(1)
		go func(index int) {
			defer workers.Done()
			request := input
			request.OperationID = fmt.Sprintf("concurrent-recovery-%d", index)
			_, err := control.CommitAgentRunRecovery(request)
			errorsSeen <- err
		}(index)
	}
	workers.Wait()
	close(errorsSeen)
	applied, conflicts := 0, 0
	for err := range errorsSeen {
		switch {
		case err == nil:
			applied++
		case errors.Is(err, ErrRevisionConflict):
			conflicts++
		default:
			t.Fatal(err)
		}
	}
	if applied != 1 || conflicts != 1 || countControlEvents(t, control) != count+3 {
		t.Fatal("racing recoveries created duplicate generations", applied, conflicts)
	}
}

func TestRecoveryAdmissionRollsBackEveryHeaderAndRequestWhenALaterWriteFails(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	before, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	count := countControlEvents(t, control)
	_, err = control.db.Exec(fmt.Sprintf(`CREATE TRIGGER recovery_write_fault BEFORE INSERT ON control_events
        WHEN NEW.domain='agent_run' AND NEW.record_id='run_events' AND NEW.revision=%d
        BEGIN SELECT RAISE(FAIL,'injected recovery write failure'); END`, before.NextIndex+2))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.CommitAgentRunRecovery(input); err == nil || !strings.Contains(err.Error(), "injected recovery write failure") {
		t.Fatal("transaction fault did not reach the actual second write", err)
	}
	after, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !reflect.DeepEqual(before, after) || countControlEvents(t, control) != count {
		t.Fatal("partial recovery escaped rollback", err)
	}
	if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
		t.Fatal("rolled back recovery retained a receipt", err)
	}
	if _, err := control.db.Exec("DROP TRIGGER recovery_write_fault"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.CommitAgentRunRecovery(input); err != nil {
		t.Fatal("unapplied transaction could not be retried", err)
	}
}

func TestRecoveryRerunWithoutResynthesisLeavesTheFinalResetCursorUnchanged(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	input.Selection = &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN,
		AgentId: " coder ", Resynthesize: proto.Bool(false)}
	preview, err := control.PreviewAgentRunRecovery(input.AgentRunRecoveryExpected)
	if err != nil || preview.Context.Selection.AgentId != "coder" || preview.Context.Synthesize || preview.Context.ResetFinal {
		t.Fatal("rerun selection/default changed", preview, err)
	}
	input.ContextSHA256, input.EventBodies = preview.ContextSHA256, input.EventBodies[:2]
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil || len(result.Events) != 2 || result.Events[1].Type != "agent_reset" || result.Run.FinalAfter != -1 ||
		result.Run.Recovery.Metadata.Execute[0].Id != "coder" || result.Run.Recovery.Metadata.Selection.AgentId != "coder" {
		t.Fatal("single Agent rerun reset the final answer at admission", result, err)
	}
	if input.Selection.AgentId != " coder " {
		t.Fatal("normalization mutated caller-owned selection")
	}
}

func TestRecoveryMetadataCannotDowngradeOrForgetCancellationGeneration(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"schema", "context", "digest", "previous-request", "runtime", "cancel-generation"} {
		t.Run(name, func(t *testing.T) {
			encoded, _ := json.Marshal(result.Run)
			var run agent.RunMetadata
			if json.Unmarshal(encoded, &run) != nil {
				t.Fatal("cannot detach run")
			}
			switch name {
			case "schema":
				run.SchemaVersion = 2
			case "context":
				run.Recovery = nil
			case "digest":
				run.Recovery.ContextSHA256 = strings.Repeat("0", 64)
			case "previous-request":
				run.Recovery.Metadata.ExpectedRequest = recoveryArtifact(run.Request)
			case "runtime":
				run.RuntimeRevision++
			case "cancel-generation":
				run.CancellationGeneration = 2
			}
			payload, _ := json.Marshal(run)
			if _, err := decodeAgentMetadata(Record{ID: run.RunID, State: run.Status, Revision: run.NextIndex,
				ExecutionEpoch: run.LastEvent.ExecutionEpoch, Payload: payload}); err == nil {
				t.Fatal("malformed schema3 metadata was accepted")
			}
		})
	}
	if err := applyAgentRuntimeUpdateTx(nil, &result.Run, AgentRunEventMutation{RuntimeUpdate: &AgentRunRuntimeUpdate{}}); !errors.Is(err, ErrIllegalTransition) {
		t.Fatal("legacy runtime swap can downgrade a recovery scope", err)
	}
}
