package api

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

// Offline signed Go fixtures reproduce selection of a terminal prior action;
// only the actual Rust/provider process matrix can qualify runtime recovery.
func failedSynthesisResumeFixture(t *testing.T) (*agentRunDriver, *driverFixtureWorker, agent.RunMetadata, store.Record) {
	t.Helper()
	driver, fixture, run := driverFailureFixture(t, "synthesizer")
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if run.Status != "failed" || len(fixture.calls) != 4 {
		t.Fatal("original conclusive synthesis failure was not retained")
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil {
		t.Fatal(err)
	}
	var failed store.Record
	for _, record := range records {
		if record.State == "FAILED" {
			failed = record
		}
	}
	if failed.ID == "" {
		t.Fatal("missing original typed failure")
	}
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	expected := store.AgentRunRecoveryExpected{RunID: run.RunID,
		Fence: &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch}, ExpectedIndex: run.NextIndex,
		ExpectedRuntimeRevision: run.RuntimeVersion(), ExpectedRequest: run.Request,
		Selection: &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME}}
	preview, err := driver.service.control.PreviewAgentRunRecovery(expected)
	if err != nil {
		t.Fatal(err)
	}
	if len(preview.Context.Execute) != 0 || !preview.Context.Synthesize {
		t.Fatal("resume did not retain completed tasks")
	}
	result, err := driver.service.control.CommitAgentRunRecovery(store.CommitAgentRunRecoveryRequest{AgentRunRecoveryExpected: expected,
		OperationID: "resume-proved-synthesis-failure", Actor: agentDriverActor, ContextSHA256: preview.ContextSHA256,
		Context: agent.ArtifactRef{SHA256: strings.Repeat("e", 64), Length: 500}, Request: agent.ArtifactRef{SHA256: strings.Repeat("d", 64), Length: 140},
		EventBodies: []agent.ArtifactRef{{SHA256: strings.Repeat("1", 64), Length: 30}, {SHA256: strings.Repeat("2", 64), Length: 30}}})
	if err != nil {
		t.Fatal(err)
	}
	return driver, fixture, result.Run, failed
}

func TestDriverExplicitSynthesisResumeUsesFreshActionAndPreservesOriginalFailure(t *testing.T) {
	driver, fixture, run, failed := failedSynthesisResumeFixture(t)
	phase, existing, err := driver.phase(run)
	if err != nil || existing != nil || phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS {
		t.Fatal("proved prior failure blocked a committed synthesis-only recovery", phase, err)
	}
	fixture.rejectNode = ""
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal("fresh explicit recovery did not complete", err)
	}
	after := driverFixtureRun(t, driver, run.RunID)
	if after.Status != "done" || strings.Join(fixture.calls, ",") != "leader,coder,critic,synthesizer,synthesizer" {
		t.Fatal("recovery redispatched retained tasks or failed to synthesize", after.Status, fixture.calls)
	}
	retained, found, err := driver.service.control.Get("action", failed.ID)
	if err != nil || !found || !reflect.DeepEqual(retained, failed) {
		t.Fatal("fresh recovery rewrote original failed authority or custody", err)
	}
}

func TestDriverExplicitPlanFailureResumeRetainsLeaderProofAndChoosesOnlyTasks(t *testing.T) {
	driver, fixture, run := driverFailureFixture(t, "leader")
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if run.Status != "failed" || strings.Join(fixture.calls, ",") != "leader" {
		t.Fatal("leader rejection was not retained before recovery")
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 1 || records[0].State != "FAILED" {
		t.Fatal("missing original failed leader", err)
	}
	failed := records[0]
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	expected := store.AgentRunRecoveryExpected{RunID: run.RunID,
		Fence: &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch}, ExpectedIndex: run.NextIndex,
		ExpectedRuntimeRevision: run.RuntimeVersion(), ExpectedRequest: run.Request,
		Selection: &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME}}
	preview, err := driver.service.control.PreviewAgentRunRecovery(expected)
	if err != nil || len(preview.Context.Execute) != 4 || len(preview.Context.ResetIds) != 4 || !preview.Context.Synthesize || !preview.Context.ResetFinal {
		t.Fatal("failed plan did not retain the legacy default task selection", err)
	}
	result, err := driver.service.control.CommitAgentRunRecovery(store.CommitAgentRunRecoveryRequest{AgentRunRecoveryExpected: expected,
		OperationID: "resume-proved-leader-failure", Actor: agentDriverActor, ContextSHA256: preview.ContextSHA256,
		Context: agent.ArtifactRef{SHA256: strings.Repeat("e", 64), Length: 500}, Request: agent.ArtifactRef{SHA256: strings.Repeat("d", 64), Length: 140},
		// Running, four default task resets and final reset are distinct public
		// events. A synthesis-only resume has only two and is a different scope.
		EventBodies: []agent.ArtifactRef{
			{SHA256: strings.Repeat("1", 64), Length: 30}, {SHA256: strings.Repeat("2", 64), Length: 30},
			{SHA256: strings.Repeat("3", 64), Length: 30}, {SHA256: strings.Repeat("4", 64), Length: 30},
			{SHA256: strings.Repeat("5", 64), Length: 30}, {SHA256: strings.Repeat("6", 64), Length: 30}}})
	if err != nil {
		t.Fatal(err)
	}
	phase, existing, err := driver.phase(result.Run)
	if err != nil || existing != nil || phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS {
		t.Fatal("proved leader failure blocked tasks or caused replanning", phase, err)
	}
	retained, found, err := driver.service.control.Get("action", failed.ID)
	if err != nil || !found || !reflect.DeepEqual(retained, failed) || len(fixture.calls) != 1 {
		t.Fatal("selecting explicit tasks rewrote or redispatched the failed leader", err)
	}
}

func TestDriverCannotRetireAnOriginalFailureAfterLosingTheControlStore(t *testing.T) {
	driver, fixture, run, failed := failedSynthesisResumeFixture(t)
	before := len(fixture.calls)
	if err := driver.service.control.Close(); err != nil {
		t.Fatal(err)
	}
	if err := driver.verifiedPriorFailure(run, failed); err == nil || len(fixture.calls) != before {
		t.Fatal("unavailable authoritative history admitted a provider retry", err)
	}
}

func TestDriverCannotRetireFailedActionWithInventedOrReboundRecoveryMetadata(t *testing.T) {
	for _, fault := range []string{"no-scope", "no-metadata", "source-index", "request", "context", "source-payload", "unknown-state", "task-phase"} {
		t.Run(fault, func(t *testing.T) {
			driver, fixture, run, failed := failedSynthesisResumeFixture(t)
			original := driverFixtureRun(t, driver, run.RunID)
			run.Recovery.Metadata = proto.Clone(run.Recovery.Metadata).(*agentv1.AgentRunRecoveryContext)
			switch fault {
			case "no-scope":
				run.Recovery = nil
			case "no-metadata":
				run.Recovery.Metadata = nil
			case "source-index":
				run.Recovery.StartIndex = 1
			case "request":
				run.Request.SHA256 = strings.Repeat("f", 64)
			case "context":
				run.Recovery.Metadata.PriorEffects = nil
			case "source-payload":
				var fields map[string]json.RawMessage
				if err := json.Unmarshal(failed.Payload, &fields); err != nil {
					t.Fatal(err)
				}
				fields["rebound"] = json.RawMessage("true")
				failed.Payload, _ = json.Marshal(fields)
			case "task-phase":
				var fields map[string]json.RawMessage
				var intent map[string]json.RawMessage
				if err := json.Unmarshal(failed.Payload, &fields); err != nil {
					t.Fatal(err)
				}
				if err := json.Unmarshal(fields["nativeAgentExecution"], &intent); err != nil {
					t.Fatal(err)
				}
				intent["phase"] = json.RawMessage(`"tasks"`)
				fields["nativeAgentExecution"], _ = json.Marshal(intent)
				failed.Payload, _ = json.Marshal(fields)
			case "unknown-state":
				failed.State = "EFFECT_UNKNOWN"
			}
			if err := driver.verifiedPriorFailure(run, failed); err == nil {
				t.Fatal("unproved or rebound history permitted retiring an unresolved action", fault)
			}
			if !reflect.DeepEqual(original, driverFixtureRun(t, driver, original.RunID)) || len(fixture.calls) != 4 {
				t.Fatal("refused recovery changed durable metadata or issued a provider retry")
			}
		})
	}
}

func TestDriverSecondExplicitSynthesisResumeKeepsBothOriginalFailureGenerations(t *testing.T) {
	driver, fixture, run, firstFailure := failedSynthesisResumeFixture(t)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal("first explicit retry could not retain a second proved rejection", err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if run.Status != "failed" || len(fixture.calls) != 5 {
		t.Fatal("second conclusive failure was not retained")
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil {
		t.Fatal(err)
	}
	before := map[string]store.Record{}
	for _, record := range records {
		if record.State == "FAILED" {
			before[record.ID] = record
		}
	}
	if len(before) != 2 || !reflect.DeepEqual(before[firstFailure.ID], firstFailure) {
		t.Fatal("first failure was replaced")
	}
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	expected := store.AgentRunRecoveryExpected{RunID: run.RunID,
		Fence: &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch}, ExpectedIndex: run.NextIndex,
		ExpectedRuntimeRevision: run.RuntimeVersion(), ExpectedRequest: run.Request,
		Selection: &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME}}
	preview, err := driver.service.control.PreviewAgentRunRecovery(expected)
	if err != nil {
		t.Fatal(err)
	}
	if preview.Context.PreviousScope == nil || preview.Context.Generation != 2 || len(preview.Context.Execute) != 0 {
		t.Fatal("second resume erased its source scope")
	}
	result, err := driver.service.control.CommitAgentRunRecovery(store.CommitAgentRunRecoveryRequest{AgentRunRecoveryExpected: expected,
		OperationID: "resume-proved-synthesis-failure-again", Actor: agentDriverActor, ContextSHA256: preview.ContextSHA256,
		Context: agent.ArtifactRef{SHA256: strings.Repeat("f", 64), Length: 500}, Request: agent.ArtifactRef{SHA256: strings.Repeat("c", 64), Length: 140},
		EventBodies: []agent.ArtifactRef{{SHA256: strings.Repeat("3", 64), Length: 30}, {SHA256: strings.Repeat("4", 64), Length: 30}}})
	if err != nil {
		t.Fatal(err)
	}
	forged := firstFailure
	forged.Payload = append(append(json.RawMessage(nil), firstFailure.Payload...), ' ')
	if err := driver.verifiedPriorFailure(result.Run, forged); err == nil {
		t.Fatal("previous-scope shortcut accepted a rebound original record")
	}
	fixture.rejectNode = ""
	if err := driver.drive(context.Background(), result.Run); err != nil {
		t.Fatal("previous-scope failure blocked second explicit recovery", err)
	}
	if driverFixtureRun(t, driver, run.RunID).Status != "done" || len(fixture.calls) != 6 {
		t.Fatal("second resume did not synthesize exactly once")
	}
	for id, original := range before {
		record, found, err := driver.service.control.Get("action", id)
		if err != nil || !found || !reflect.DeepEqual(original, record) {
			t.Fatal("recovery rewrote a preceding failure", id, err)
		}
	}
}
