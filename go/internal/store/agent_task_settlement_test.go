package store

import (
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func taskSettlementFixture(t *testing.T, failed bool, historical ...bool) (*Control, AgentTaskSettlementRequest, ed25519.PrivateKey) {
	t.Helper()
	control, source, private := agentOutcomeFixture(t)
	if len(historical) == 0 {
		source.Receipt.Evidence.TaskOutput = &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 13, CompletionTokens: 21, LatencyMs: 120, Failed: failed}
	}
	agentOutcomeSign(t, source.Receipt, private)
	recorded, err := control.RecordAgentNodeOutcome(source)
	if err != nil {
		t.Fatal(err)
	}
	lease, found, err := control.GetActionLease(source.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	return control, AgentTaskSettlementRequest{OperationID: "settle-task-coder", Actor: source.Actor, ActionID: source.ActionID, Epoch: source.Epoch,
		RunID: source.RunID, NodeID: source.NodeID, Owner: lease.Owner, ClaimToken: lease.ClaimToken, ExpectedIndex: 4, SourceReceiptSHA256: recorded.ReceiptSHA256}, private
}

func taskDependentRequest(input AgentTaskSettlementRequest) AgentNodeDispatchRequest {
	return AgentNodeDispatchRequest{OperationID: "begin-dependent-critic", Actor: input.Actor, ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID,
		NodeID: "critic", Owner: input.Owner, ClaimToken: input.ClaimToken}
}

func TestTaskSettlementCommitsCompletionAndVerifiedDependencyWithoutReleasingPhase(t *testing.T) {
	control, input, _ := taskSettlementFixture(t, false)
	before := countControlEvents(t, control)
	if _, err := control.BeginAgentNodeDispatch(taskDependentRequest(input)); !errors.Is(err, ErrAgentNodeDependencyUnverified) {
		t.Fatalf("receipt alone authorized dependency: %v", err)
	}
	result, err := control.SettleAgentTask(input)
	if err != nil || result.AlreadyApplied || result.Action.State != "EXECUTING" || result.Run.NextIndex != 5 || countControlEvents(t, control) != before+2 {
		t.Fatalf("atomic task settlement: %+v %v", result, err)
	}
	node := result.Run.Nodes()["coder"]
	if node.State != "succeeded" || node.PromptTokens != 13 || node.CompletionTokens != 21 || node.LatencyMS == nil || *node.LatencyMS != 120 {
		t.Fatalf("signed counters were not projected: %+v", node)
	}
	lease, found, err := control.GetActionLease(input.ActionID)
	if err != nil || !found {
		t.Fatalf("task completed the whole phase: %+v %v", lease, err)
	}
	var active int
	if err := control.db.QueryRow("SELECT count(*) FROM action_leases WHERE action_id=? AND terminal_state IS NULL", input.ActionID).Scan(&active); err != nil || active != 1 {
		t.Fatalf("task terminated phase lease: %v", err)
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 1 {
		t.Fatalf("task released phase resources: %v", err)
	}
	replay, err := control.SettleAgentTask(input)
	if err != nil || !replay.AlreadyApplied || countControlEvents(t, control) != before+2 {
		t.Fatalf("task replay wrote another event: %v", err)
	}
	dependent, err := control.BeginAgentNodeDispatch(taskDependentRequest(input))
	if err != nil || !dependent.Fresh || dependent.Intent.Task != "review" || len(dependent.Intent.DependsOn) != 1 {
		t.Fatalf("verified dependency not admitted: %+v %v", dependent, err)
	}
	repeated, err := control.BeginAgentNodeDispatch(taskDependentRequest(input))
	if err != nil || repeated.Fresh {
		t.Fatalf("dependent replay authorized another provider send: %v", err)
	}
	input.SourceReceiptSHA256 = strings.Repeat("9", 64)
	if _, err := control.SettleAgentTask(input); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("rebound settlement operation: %v", err)
	}
}

func TestSignedFailedTaskCanInformDependentsButHistoricalReceiptCannot(t *testing.T) {
	for _, name := range []string{"failed", "historical-without-counters"} {
		t.Run(name, func(t *testing.T) {
			var control *Control
			var input AgentTaskSettlementRequest
			if name == "failed" {
				control, input, _ = taskSettlementFixture(t, true)
			} else {
				control, input, _ = taskSettlementFixture(t, false, true)
			}
			before := countControlEvents(t, control)
			result, err := control.SettleAgentTask(input)
			if name == "failed" {
				if err != nil || result.Run.Nodes()["coder"].State != "failed" || !result.Run.LastEvent.Failed {
					t.Fatalf("failed receipt projected as success: %v", err)
				}
			} else if !errors.Is(err, ErrAgentNodeOutcomeRequired) || countControlEvents(t, control) != before {
				t.Fatalf("historical proof fabricated counters/completion: %v", err)
			}
			next, dispatchErr := control.BeginAgentNodeDispatch(taskDependentRequest(input))
			if name == "failed" {
				if dispatchErr != nil || !next.Fresh {
					t.Fatalf("signed failure did not inform dependent work: %v", dispatchErr)
				}
			} else if !errors.Is(dispatchErr, ErrAgentNodeDependencyUnverified) {
				t.Fatalf("historical incomplete predecessor was admitted: %v", dispatchErr)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatal("unresolved phase resource lost")
			}
		})
	}
}

func TestTaskSettlementRefusalsLeaveNoPartialCompletion(t *testing.T) {
	cases := map[string]func(*Control, *AgentTaskSettlementRequest){
		"invalid-action":   func(_ *Control, in *AgentTaskSettlementRequest) { in.ActionID = "" },
		"invalid-run":      func(_ *Control, in *AgentTaskSettlementRequest) { in.RunID = "" },
		"invalid-node":     func(_ *Control, in *AgentTaskSettlementRequest) { in.NodeID = "" },
		"zero-epoch":       func(_ *Control, in *AgentTaskSettlementRequest) { in.Epoch = 0 },
		"overflow-epoch":   func(_ *Control, in *AgentTaskSettlementRequest) { in.Epoch = math.MaxUint64 },
		"invalid-cursor":   func(_ *Control, in *AgentTaskSettlementRequest) { in.ExpectedIndex = 0 },
		"overflow-cursor":  func(_ *Control, in *AgentTaskSettlementRequest) { in.ExpectedIndex = math.MaxInt64 },
		"invalid-source":   func(_ *Control, in *AgentTaskSettlementRequest) { in.SourceReceiptSHA256 = "wrong" },
		"different-source": func(_ *Control, in *AgentTaskSettlementRequest) { in.SourceReceiptSHA256 = strings.Repeat("9", 64) },
		"different-owner":  func(_ *Control, in *AgentTaskSettlementRequest) { in.Owner = "another-owner" },
		"different-token":  func(_ *Control, in *AgentTaskSettlementRequest) { in.ClaimToken = "another-token" },
		"missing-outcome":  func(_ *Control, in *AgentTaskSettlementRequest) { in.NodeID = "critic" },
		"stale-cursor":     func(_ *Control, in *AgentTaskSettlementRequest) { in.ExpectedIndex++ },
		"stale-epoch":      func(_ *Control, in *AgentTaskSettlementRequest) { in.Epoch++ },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			control, input, _ := taskSettlementFixture(t, false)
			original := input
			events := countControlEvents(t, control)
			change(control, &input)
			if _, err := control.SettleAgentTask(input); err == nil {
				t.Fatal("invalid settlement was applied")
			}
			run, _, err := control.AgentRunMetadata(original.RunID)
			if err != nil || run.NextIndex != 4 || countControlEvents(t, control) != events {
				t.Fatalf("rejected input left partial events: %v", err)
			}
			if _, found, err := control.ReadOperatorMutation(original.OperationID); err != nil || found {
				t.Fatal("refused input acquired a receipt")
			}
			if _, err := control.SettleAgentTask(original); err != nil {
				t.Fatalf("refusal poisoned valid retry: %v", err)
			}
		})
	}
}

func TestTaskSettlementSQLFailureAndIgnoredUpdatesRollBackBothRecords(t *testing.T) {
	for _, name := range []string{"abort-action", "ignore-action", "ignore-run"} {
		t.Run(name, func(t *testing.T) {
			control, input, _ := taskSettlementFixture(t, false)
			events := countControlEvents(t, control)
			table, mode := "action_journal", "ABORT,'fixture failure'"
			if name == "ignore-action" {
				mode = "IGNORE"
			}
			if name == "ignore-run" {
				table, mode = "agent_runs", "IGNORE"
			}
			query := fmt.Sprintf("CREATE TEMP TRIGGER task_settlement_fault BEFORE UPDATE ON %s BEGIN SELECT RAISE(%s); END", table, mode)
			if _, err := control.db.Exec(query); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentTask(input); err == nil {
				t.Fatal("lost SQL write was accepted")
			}
			if _, err := control.db.Exec("DROP TRIGGER task_settlement_fault"); err != nil {
				t.Fatal(err)
			}
			run, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil || run.NextIndex != 4 || countControlEvents(t, control) != events {
				t.Fatalf("partial task transaction retained: %v", err)
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatal("failed SQL acquired replay state")
			}
			if _, err := control.SettleAgentTask(input); err != nil {
				t.Fatalf("SQL fault poisoned retry: %v", err)
			}
		})
	}
}

func TestTaskSettlementMarkerCannotRebindProofWriterCursorOrMetadata(t *testing.T) {
	control, input, _ := taskSettlementFixture(t, false)
	result, err := control.SettleAgentTask(input)
	if err != nil {
		t.Fatal(err)
	}
	mutations := map[string]func(*AgentTaskSettlement){
		"source":         func(m *AgentTaskSettlement) { m.SourceReceiptSHA256 = strings.Repeat("8", 64) },
		"cursor":         func(m *AgentTaskSettlement) { m.MetadataIndex++ },
		"metadata-epoch": func(m *AgentTaskSettlement) { m.MetadataEpoch++ },
		"writer":         func(m *AgentTaskSettlement) { m.WriterFencingToken++ },
		"revision":       func(m *AgentTaskSettlement) { m.ActionRevision-- },
		"time":           func(m *AgentTaskSettlement) { m.RecordedAt++ },
		"schema":         func(m *AgentTaskSettlement) { m.SchemaVersion = 2 },
	}
	for name, change := range mutations {
		t.Run(name, func(t *testing.T) {
			record := result.Action
			markers, err := readAgentTaskSettlements(record)
			if err != nil {
				t.Fatal(err)
			}
			marker := markers["coder"]
			change(&marker)
			markers["coder"] = marker
			var fields map[string]json.RawMessage
			_ = json.Unmarshal(record.Payload, &fields)
			fields[agentTaskSettlementField], _ = json.Marshal(markers)
			record.Payload, _ = json.Marshal(fields)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			_, err = control.validatedAgentTaskSettlementsTx(tx, record)
			_ = tx.Rollback()
			if err == nil {
				t.Fatal("marker rebound retained independent evidence")
			}
		})
	}
	if replay, err := control.SettleAgentTask(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("rejected read corrupted durable replay: %v", err)
	}
}

func TestTypedTaskCountersAreCoveredByIndependentCustodyVerification(t *testing.T) {
	cases := map[string]func(*agentv1.AgentTaskOutputMetadata){
		"schema":              func(m *agentv1.AgentTaskOutputMetadata) { m.SchemaVersion = 2 },
		"negative-latency":    func(m *agentv1.AgentTaskOutputMetadata) { m.LatencyMs = -1 },
		"prompt-overflow":     func(m *agentv1.AgentTaskOutputMetadata) { m.PromptTokens = math.MaxUint64 },
		"completion-overflow": func(m *agentv1.AgentTaskOutputMetadata) { m.CompletionTokens = math.MaxUint64 },
		"unknown":             func(m *agentv1.AgentTaskOutputMetadata) { m.ProtoReflect().SetUnknown([]byte{0x30, 0x01}) },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			control, source, key := agentOutcomeFixture(t)
			source.Receipt.Evidence.TaskOutput = &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, LatencyMs: 1}
			change(source.Receipt.Evidence.TaskOutput)
			agentOutcomeSign(t, source.Receipt, key)
			if _, err := control.RecordAgentNodeOutcome(source); !errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) {
				t.Fatalf("invalid signed counters retained: %v", err)
			}
		})
	}
	control, source, key := agentOutcomeFixture(t)
	source.Receipt.Evidence.TaskOutput = &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, LatencyMs: 1, PromptTokens: 7}
	agentOutcomeSign(t, source.Receipt, key)
	source.Receipt.Evidence.TaskOutput.PromptTokens++
	if _, err := control.RecordAgentNodeOutcome(source); !errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) {
		t.Fatal("counter tampering survived independent verification")
	}

}

func TestTaskSettlementRequiresBothCurrentCutoverAuthorities(t *testing.T) {
	for _, domain := range []string{"action", "agent_run"} {
		t.Run(domain, func(t *testing.T) {
			control, input, _ := taskSettlementFixture(t, false)
			before := countControlEvents(t, control)
			if _, err := control.db.Exec("UPDATE control_cutover SET state=? WHERE domain=?", CutoverShadow, domain); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentTask(input); !errors.Is(err, ErrCutoverNotAuthorized) || countControlEvents(t, control) != before {
				t.Fatalf("lost authority admitted completion: %v", err)
			}
			if _, err := control.db.Exec("UPDATE control_cutover SET state=? WHERE domain=?", CutoverGoAuthoritative, domain); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentTask(input); err != nil {
				t.Fatalf("authority refusal poisoned retry: %v", err)
			}
		})
	}
}

func TestTaskSettlementRechecksActionAndWriterDeadlineAtCommit(t *testing.T) {
	for _, name := range []string{"action", "writer"} {
		t.Run(name, func(t *testing.T) {
			control, input, _ := taskSettlementFixture(t, false)
			before := countControlEvents(t, control)
			clock := control.now
			start := clock()
			deadline := control.leaseUntil
			expected := ErrWriterFenceHeld
			if name == "action" {
				lease, _, err := control.GetActionLease(input.ActionID)
				if err != nil {
					t.Fatal(err)
				}
				deadline = lease.LeaseUntil
				control.leaseSeconds = deadline - start + 120
				expected = ErrActionLeaseExpired
			} else {
				deadline = start + control.leaseSeconds
			}
			calls := 0
			control.now = func() int64 {
				calls++
				if calls == 1 {
					return start
				}
				return deadline
			}
			_, err := control.SettleAgentTask(input)
			control.now = clock
			if !errors.Is(err, expected) || countControlEvents(t, control) != before {
				t.Fatalf("expired %s commit retained completion: %v", name, err)
			}
			run, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil || run.NextIndex != 4 {
				t.Fatal("expired commit retained output")
			}
			if _, err := control.SettleAgentTask(input); err != nil {
				t.Fatalf("expired commit poisoned retry: %v", err)
			}
		})
	}
}

func TestTaskSettlementPrivateMarkersCannotBeChangedByGenericWrite(t *testing.T) {
	control, input, _ := taskSettlementFixture(t, false)
	result, err := control.SettleAgentTask(input)
	if err != nil {
		t.Fatal(err)
	}
	record := result.Action
	record.Revision++
	var fields map[string]json.RawMessage
	_ = json.Unmarshal(record.Payload, &fields)
	delete(fields, agentTaskSettlementField)
	record.Payload, _ = json.Marshal(fields)
	write, err := prepareControlRecordWrite(record, nil)
	if err != nil {
		t.Fatal(err)
	}
	write.allowBoundLease = true
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	err = control.putControlRecordTx(tx, write, control.now())
	_ = tx.Rollback()
	if !errors.Is(err, ErrAgentRunExecutionRequired) {
		t.Fatalf("generic write deleted a task settlement: %v", err)
	}
	if replay, err := control.SettleAgentTask(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("generic refusal damaged original completion: %v", err)
	}
}

func TestTaskSettlementFencingMetadataPreservesSecretRejection(t *testing.T) {
	control, input, _ := taskSettlementFixture(t, false)
	result, err := control.SettleAgentTask(input)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(result.Action.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	var markers map[string]map[string]json.RawMessage
	if err := json.Unmarshal(fields[agentTaskSettlementField], &markers); err != nil {
		t.Fatal(err)
	}
	var writer int64
	if err := json.Unmarshal(markers[input.NodeID]["fencingToken"], &writer); err != nil || writer != control.token {
		t.Fatalf("writer fence was not retained as numeric fencing metadata: %d %v", writer, err)
	}
	if _, present := markers[input.NodeID]["writerFencingToken"]; present {
		t.Fatal("settlement persisted the secret-bearing legacy field name")
	}
	before := countControlEvents(t, control)
	for _, key := range []string{"writerFencingToken", "claimToken", "bearerToken", "privateKey"} {
		t.Run(key, func(t *testing.T) {
			var payload map[string]any
			if err := json.Unmarshal(result.Action.Payload, &payload); err != nil {
				t.Fatal(err)
			}
			nested := payload[agentTaskSettlementField].(map[string]any)[input.NodeID].(map[string]any)
			// Numeric values must not exempt secret-bearing keys from rejection.
			nested[key] = int64(1)
			record := result.Action
			record.Revision++
			record.Payload, err = json.Marshal(payload)
			if err != nil {
				t.Fatal(err)
			}
			if err := control.Put(record); !errors.Is(err, ErrSecretDetected) {
				t.Fatalf("secret-bearing nested field admitted: %v", err)
			}
			if countControlEvents(t, control) != before {
				t.Fatal("secret rejection retained a partial control event")
			}
		})
	}
	if replay, err := control.SettleAgentTask(input); err != nil || !replay.AlreadyApplied || replay.Run.NextIndex != 5 {
		t.Fatalf("secret rejection damaged retained completion: %v", err)
	}
	if _, err := control.BeginAgentNodeDispatch(taskDependentRequest(input)); err != nil {
		t.Fatalf("numeric fencing metadata did not authorize verified dependency: %v", err)
	}
}
