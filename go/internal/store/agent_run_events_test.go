package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

func freshAgentEventControl(t *testing.T) *Control {
	t.Helper()
	control := openAuthority(t)
	checkpoint := emptyPythonInventoryCheckpoint(t)
	if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "agent_run")
	request := CutoverTransition{Domain: "agent_run", To: CutoverGoAuthoritative, ExpectedRevision: dual.Revision,
		ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken, TransferID: "fresh-agent-events", Authority: checkpoint}
	var err error
	request.Promotion, err = SignPromotionArtifact(PromotionArtifactForTransition(request, dual, control.now(), "fleet-a", "production"), promotionTestPrivate)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(request); err != nil {
		t.Fatal(err)
	}
	return control
}

func eventMutation(t *testing.T, control *Control, index int64, event agent.RunEvent) AgentRunEventMutation {
	t.Helper()
	current, err := control.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	event.Body = agent.ArtifactRef{SHA256: strings.Repeat("a", 64), Length: 30}
	return AgentRunEventMutation{OperationID: fmt.Sprintf("run-event-%d", index), Actor: "operator:event-test", RunID: "run_events", ExpectedIndex: index,
		Fence: &internalprotocol.ActionFence{ActionId: "run_events", ExecutionEpoch: uint64(current.Epoch)},
		Create: &agent.RunMetadata{Preset: "full", ConfirmPlan: true, ConversationID: "chat", MessageID: "message",
			Request: agent.ArtifactRef{SHA256: strings.Repeat("b", 64), Length: 100}}, Event: event}
}

func TestAgentEventsPersistCursorReplayAndDagWithoutExecutionAuthority(t *testing.T) {
	control := freshAgentEventControl(t)
	t.Cleanup(func() {
		if control != nil {
			_ = control.Close()
		}
	})
	events := []agent.RunEvent{{Type: "run_status", Status: "created"}, {Type: "run_status", Status: "planning"},
		{Type: "agent_plan", Plan: []agent.RunPlanNode{{ID: "coder", Task: "code"}, {ID: "critic", Task: "review", DependsOn: []string{"coder"}}}},
		{Type: "run_status", Status: "awaiting_plan"}, {Type: "run_status", Status: "running"},
		{Type: "agent", Phase: "coder", Status: "running"}, {Type: "agent_output", Phase: "coder", PromptTokens: 12, CompletionTokens: 8},
		{Type: "agent_delta", Phase: "critic"}, {Type: "agent_reset", Phase: "coder"}}
	for index, event := range events {
		mutation := eventMutation(t, control, int64(index), event)
		if index != 0 {
			mutation.Create = nil
		}
		result, err := control.AppendAgentRunEvent(mutation)
		if err != nil || result.Event.Index != int64(index) || result.Run.NextIndex != int64(index+1) {
			t.Fatalf("append %d: %+v %v", index, result, err)
		}
		count := countControlEvents(t, control)
		replay, err := control.AppendAgentRunEvent(mutation)
		if err != nil || replay.Mutation.Status != OperatorMutationAlreadyApplied || replay.Event.Index != int64(index) || countControlEvents(t, control) != count {
			t.Fatalf("replay: %+v %v", replay, err)
		}
		if _, exists, err := control.GetActionLease("run_events"); err != nil || exists {
			t.Fatalf("metadata granted execution: %v %v", exists, err)
		}
	}
	run, found, err := control.AgentRunMetadata("run_events")
	if err != nil || !found || run.Nodes()["coder"].State != "retrying" || run.Nodes()["critic"].State != "created" {
		t.Fatalf("DAG replay: %+v %v %v", run, found, err)
	}
	page, err := control.AgentRunEventsAfter("run_events", 5, 2)
	if err != nil || len(page) != 2 || page[0].Index != 6 || page[1].Index != 7 {
		t.Fatalf("cursor: %+v %v", page, err)
	}
	options := OpenOptions{Path: control.path, Owner: "agent-events-successor", Now: control.now, AuthorizeCutover: true,
		PromotionSignerPublicKey: control.promotionSignerKey, FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	control, err = OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	after, found, err := control.AgentRunMetadata("run_events")
	beforeJSON, _ := json.Marshal(run)
	afterJSON, _ := json.Marshal(after)
	if err != nil || !found || string(beforeJSON) != string(afterJSON) {
		t.Fatalf("reopen changed snapshot: %v %v", found, err)
	}
	if page, err := control.AgentRunEventsAfter("run_events", 8, 10); err != nil || len(page) != 0 {
		t.Fatalf("end cursor: %+v %v", page, err)
	}
}

func TestAgentEventAppendRejectsAuthorityCursorAndReplayConflicts(t *testing.T) {
	control := openAuthority(t)
	mutation := eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "created"})
	if _, err := control.AppendAgentRunEvent(mutation); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("unpromoted write: %v", err)
	}
	_ = control.Close()
	control = freshAgentEventControl(t)
	defer control.Close()
	mutation = eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "created"})
	if _, err := control.AppendAgentRunEvent(mutation); err != nil {
		t.Fatal(err)
	}
	count := countControlEvents(t, control)
	for _, changed := range []AgentRunEventMutation{
		func() AgentRunEventMutation { r := mutation; r.Actor = "operator:another"; return r }(),
		func() AgentRunEventMutation { r := mutation; r.Event.Body.SHA256 = strings.Repeat("c", 64); return r }(),
	} {
		if _, err := control.AppendAgentRunEvent(changed); !errors.Is(err, ErrMutationRequestReplayConflict) {
			t.Fatalf("conflicting replay: %v", err)
		}
	}
	stale := eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "planning"})
	stale.Create = nil
	stale.OperationID = "stale-cursor"
	if _, err := control.AppendAgentRunEvent(stale); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("stale cursor: %v", err)
	}
	if countControlEvents(t, control) != count {
		t.Fatal("refusal appended history")
	}
}

func TestAgentEventAtomicRollbackAndGenericWriterDenial(t *testing.T) {
	control := freshAgentEventControl(t)
	defer control.Close()
	if _, err := control.AppendAgentRunEvent(eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "created"})); err != nil {
		t.Fatal(err)
	}
	record, _, err := control.Get("agent_run", "run_events")
	if err != nil {
		t.Fatal(err)
	}
	for _, body := range []json.RawMessage{record.Payload, json.RawMessage(`{"runId":"run_events"}`)} {
		attempt := OperatorMutation{OperationID: "generic-agent-bypass", ActionID: "run_events", Actor: "operator:event-test", Record: record}
		attempt.Record.Payload = body
		attempt.Record.Revision++
		if _, err := control.ApplyOperatorMutation(attempt); !errors.Is(err, ErrAgentRunEventRequired) {
			t.Fatalf("generic writer bypass: %v", err)
		}
	}
	count := countControlEvents(t, control)
	if _, err := control.db.Exec(`CREATE TRIGGER reject_agent_operation BEFORE INSERT ON control_operator_mutations BEGIN SELECT RAISE(ABORT,'agent operation fault'); END`); err != nil {
		t.Fatal(err)
	}
	mutation := eventMutation(t, control, 1, agent.RunEvent{Type: "run_status", Status: "planning"})
	mutation.Create = nil
	if _, err := control.AppendAgentRunEvent(mutation); err == nil {
		t.Fatal("operation journal failure committed")
	}
	after, _, err := control.Get("agent_run", "run_events")
	if err != nil || after.Revision != record.Revision || string(after.Payload) != string(record.Payload) || countControlEvents(t, control) != count {
		t.Fatalf("partial event transaction: %+v %v", after, err)
	}
	if _, err := control.db.Exec(`DROP TRIGGER reject_agent_operation`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AppendAgentRunEvent(mutation); err != nil {
		t.Fatal(err)
	}
}

func TestAgentEventDirectAdmissionAndMissingRuns(t *testing.T) {
	control := freshAgentEventControl(t)
	defer control.Close()
	for _, test := range []struct {
		name   string
		change func(*AgentRunEventMutation)
		want   error
	}{
		{"invalid identity", func(r *AgentRunEventMutation) { r.RunID = "../run" }, ErrAgentRunMetadataInvalid},
		{"negative cursor", func(r *AgentRunEventMutation) { r.ExpectedIndex = -1 }, ErrAgentRunMetadataInvalid},
		{"missing body", func(r *AgentRunEventMutation) { r.Event.Body = agent.ArtifactRef{} }, ErrAgentRunMetadataInvalid},
		{"empty type", func(r *AgentRunEventMutation) { r.Event.Type = "" }, ErrAgentRunMetadataInvalid},
		{"missing fence", func(r *AgentRunEventMutation) { r.Fence = nil }, internalprotocol.ErrFenceMismatch},
		{"wrong identity fence", func(r *AgentRunEventMutation) { r.Fence.ActionId = "other" }, internalprotocol.ErrFenceMismatch},
		{"future epoch", func(r *AgentRunEventMutation) { r.Fence.ExecutionEpoch++ }, internalprotocol.ErrFenceMismatch},
		{"zero epoch", func(r *AgentRunEventMutation) { r.Fence.ExecutionEpoch = 0 }, internalprotocol.ErrZeroEpoch},
		{"missing create", func(r *AgentRunEventMutation) { r.Create = nil }, ErrAgentRunMetadataInvalid},
		{"invalid initial status", func(r *AgentRunEventMutation) { r.Event.Status = "planning" }, ErrAgentRunMetadataInvalid},
		{"missing request reference", func(r *AgentRunEventMutation) { r.Create.Request = agent.ArtifactRef{} }, ErrAgentRunMetadataInvalid},
		{"secret material", func(r *AgentRunEventMutation) { r.Event.Scope = "AGE-SECRET-KEY-1" + strings.Repeat("X", 48) }, ErrSecretDetected},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "created"})
			test.change(&request)
			before := countControlEvents(t, control)
			if _, err := control.AppendAgentRunEvent(request); !errors.Is(err, test.want) {
				t.Fatalf("direct admission: %v", err)
			}
			if countControlEvents(t, control) != before {
				t.Fatal("rejected admission wrote history")
			}
		})
	}
	if _, found, err := control.AgentRunMetadata("run_events"); err != nil || found {
		t.Fatalf("missing snapshot: %v %v", found, err)
	}
	if _, err := control.AgentRunEventsAfter("run_events", -1, 1); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("missing events: %v", err)
	}
	for _, request := range []struct {
		id    string
		after int64
		limit int
	}{{"../run", -1, 1}, {"run_events", -2, 1}, {"run_events", -1, 0}, {"run_events", -1, 1025}} {
		if _, err := control.AgentRunEventsAfter(request.id, request.after, request.limit); !errors.Is(err, ErrAgentRunMetadataInvalid) {
			t.Fatalf("invalid cursor: %v", err)
		}
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AgentRunEventsAfter("run_events", -1, 1); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed event reader: %v", err)
	}
}

func TestAgentEventLegacyMetadataAndCorruptHistoryFailClosed(t *testing.T) {
	control := freshAgentEventControl(t)
	defer control.Close()
	current, err := control.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	// Old, unmarked records cannot be silently reinterpreted as the new metadata.
	legacy := Record{Domain: "agent_run", ID: "run_events", State: "created", Revision: 1, ExecutionEpoch: uint64(current.Epoch), Payload: json.RawMessage(`{"legacy":true}`)}
	if _, err := control.ApplyOperatorMutation(OperatorMutation{OperationID: "legacy-agent", ActionID: legacy.ID, Actor: "operator:legacy-test", Record: legacy}); err != nil {
		t.Fatal(err)
	}
	if _, found, err := control.AgentRunMetadata(legacy.ID); !found || !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("legacy snapshot: %v %v", found, err)
	}
	if _, err := control.AgentRunEventsAfter(legacy.ID, -1, 10); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("legacy events: %v", err)
	}
	request := eventMutation(t, control, 1, agent.RunEvent{Type: "run_status", Status: "planning"})
	request.Create = nil
	before := countControlEvents(t, control)
	if _, err := control.AppendAgentRunEvent(request); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("legacy append: %v", err)
	}
	// Actual SQLite corruption in this isolated fixture must deny reads and append.
	if _, err := control.db.Exec(`UPDATE agent_runs SET record_digest=? WHERE id=?`, strings.Repeat("0", 64), legacy.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AgentRunEventsAfter(legacy.ID, -1, 10); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt events: %v", err)
	}
	if _, err := control.AppendAgentRunEvent(request); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt append: %v", err)
	}
	if countControlEvents(t, control) != before {
		t.Fatal("corruption refusal appended history")
	}
}

func TestAgentEventRowFailureAndSchemaLossFailClosed(t *testing.T) {
	control := freshAgentEventControl(t)
	defer control.Close()
	if _, err := control.db.Exec(`CREATE TRIGGER reject_agent_row BEFORE INSERT ON agent_runs BEGIN SELECT RAISE(ABORT,'agent row fault'); END`); err != nil {
		t.Fatal(err)
	}
	request := eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "created"})
	before := countControlEvents(t, control)
	if _, err := control.AppendAgentRunEvent(request); err == nil {
		t.Fatal("row fault admitted append")
	}
	if countControlEvents(t, control) != before {
		t.Fatal("row fault committed partial history")
	}
	if _, err := control.db.Exec(`DROP TRIGGER reject_agent_row`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AppendAgentRunEvent(request); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`DROP TABLE agent_runs`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AgentRunEventsAfter("run_events", -1, 1); err == nil {
		t.Fatal("schema loss returned events")
	}
}
