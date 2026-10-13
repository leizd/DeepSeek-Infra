package store

import (
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func TestTasksCompletionRejectsForgedCheckpointWriterAndSettlementBindings(t *testing.T) {
	control, input := tasksCompletionFixture(t, false)
	result, err := control.CompleteAgentTasks(input)
	if err != nil {
		t.Fatal(err)
	}
	completion, present, err := readAgentTasksCompletion(result.Action)
	if err != nil || !present {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*AgentTasksCompletion){
		"schema":             func(m *AgentTasksCompletion) { m.SchemaVersion++ },
		"settlements":        func(m *AgentTasksCompletion) { m.SettlementsSHA256 = strings.Repeat("9", 64) },
		"metadata-cursor":    func(m *AgentTasksCompletion) { m.MetadataIndex-- },
		"missing-checkpoint": func(m *AgentTasksCompletion) { m.MetadataIndex += 20 },
		"metadata-epoch":     func(m *AgentTasksCompletion) { m.MetadataEpoch++ },
		"action-revision":    func(m *AgentTasksCompletion) { m.ActionRevision-- },
		"execution-epoch":    func(m *AgentTasksCompletion) { m.ExecutionEpoch++ },
		"writer":             func(m *AgentTasksCompletion) { m.WriterFencingToken++ },
		"time":               func(m *AgentTasksCompletion) { m.RecordedAt++ },
	} {
		t.Run(name, func(t *testing.T) {
			changed := completion
			change(&changed)
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(result.Action.Payload, &fields); err != nil {
				t.Fatal(err)
			}
			fields[agentTasksCompletionField], err = json.Marshal(changed)
			if err != nil {
				t.Fatal(err)
			}
			action := result.Action
			action.Payload, err = json.Marshal(fields)
			if err != nil {
				t.Fatal(err)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentTasksCompletionTx(tx, action)
			_ = tx.Rollback()
			if err == nil {
				t.Fatal("forged phase completion binding was accepted")
			}
		})
	}
	if replay, err := control.CompleteAgentTasks(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("refused forged proof damaged valid completion: %v", err)
	}
}

func tasksCompletionFixture(t *testing.T, failed bool) (*Control, CompleteAgentTasksRequest) {
	t.Helper()
	control, input, _ := tasksCompletionSignedFixture(t, failed)
	return control, input
}

func tasksCompletionSignedFixture(t *testing.T, failed bool) (*Control, CompleteAgentTasksRequest, ed25519.PrivateKey) {
	t.Helper()
	control, first, private := taskSettlementFixture(t, false)
	if _, err := control.SettleAgentTask(first); err != nil {
		t.Fatal(err)
	}
	dependent, err := control.BeginAgentNodeDispatch(taskDependentRequest(first))
	if err != nil {
		t.Fatal(err)
	}
	_, _, receipt, found, err := control.AgentNodeOutcomeSnapshot(first.ActionID, first.NodeID)
	if err != nil || !found || receipt == nil {
		t.Fatal(err)
	}
	receipt = proto.Clone(receipt).(*agentv1.AgentNodeOutcomeReceipt)
	intent := agentOutcomeIntent(dependent.Intent)
	receipt.Evidence.Fence = intent.Fence
	receipt.Evidence.OperationId, receipt.Evidence.NodeId = intent.OperationId, intent.NodeId
	receipt.Evidence.CreatedAt = time.Unix(intent.RecordedAt, 0).UTC().Format("2006-01-02T15:04:05Z")
	receipt.Evidence.ConfirmedAt = time.Unix(intent.RecordedAt+1, 0).UTC().Format("2006-01-02T15:04:05Z")
	receipt.Evidence.TaskOutput.Failed = failed
	agentOutcomeSign(t, receipt, private)
	stored, err := control.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{OperationID: "record-task-critic", Actor: first.Actor, ActionID: first.ActionID, Epoch: first.Epoch, RunID: first.RunID, NodeID: "critic", Receipt: receipt})
	if err != nil {
		t.Fatal(err)
	}
	second := first
	second.OperationID, second.NodeID, second.ExpectedIndex, second.SourceReceiptSHA256 = "settle-task-critic", "critic", 5, stored.ReceiptSHA256
	if _, err := control.SettleAgentTask(second); err != nil {
		t.Fatal(err)
	}
	return control, CompleteAgentTasksRequest{OperationID: "complete-agent-tasks", Actor: first.Actor, ActionID: first.ActionID, Epoch: first.Epoch, RunID: first.RunID, Owner: first.Owner, ClaimToken: first.ClaimToken, ExpectedIndex: 6}, private
}

func TestTasksCompletionAtomicallyTerminatesPhaseWithoutInventingFinalOutput(t *testing.T) {
	control, input := tasksCompletionFixture(t, false)
	before := countControlEvents(t, control)
	result, err := control.CompleteAgentTasks(input)
	if err != nil || result.AlreadyApplied || result.Action.State != "SUCCEEDED" || result.Run.Status != "running" || result.Run.NextIndex != 6 || result.Run.LastEvent.Type != "agent_output" || countControlEvents(t, control) != before+1 {
		t.Fatalf("phase completion fabricated final output or lost atomicity: %+v %v", result, err)
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 0 {
		t.Fatalf("completed phase retained resources: %v", err)
	}
	var active int
	if err := control.db.QueryRow(`SELECT count(*) FROM action_leases WHERE action_id=? AND terminal_state IS NULL`, input.ActionID).Scan(&active); err != nil || active != 0 {
		t.Fatal("completed phase retained an active lease")
	}
	replay, err := control.CompleteAgentTasks(input)
	if err != nil || !replay.AlreadyApplied || replay.Run.NextIndex != 6 || countControlEvents(t, control) != before+1 {
		t.Fatalf("completion replay duplicated metadata: %v", err)
	}
	claim := agentExecutionRequest(t, control)
	claim.OperationID, claim.ExpectedIndex = "fresh-claim-after-tasks", 6
	if _, err := control.ClaimAgentRunExecution(claim); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("completed tasks authorized another fresh phase: %v", err)
	}
	if _, _, receipt, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, "critic"); err != nil || !found || receipt == nil {
		t.Fatalf("termination lost retained task proof: %v", err)
	}
	input.ExpectedIndex = 5
	if _, err := control.CompleteAgentTasks(input); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("completion operation rebound to another cursor: %v", err)
	}
}

func TestTasksCompletionRestartRevalidatesOriginalWriterAndTermination(t *testing.T) {
	control, input := tasksCompletionFixture(t, false)
	if _, err := control.CompleteAgentTasks(input); err != nil {
		t.Fatal(err)
	}
	options := OpenOptions{Path: control.path, Owner: "tasks-completion-successor", Now: control.now, AuthorizeCutover: true,
		PromotionSignerPublicKey: control.promotionSignerKey, AgentOutcomeSignerPublicKey: control.agentOutcomeSignerKey, FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	if replay, err := successor.CompleteAgentTasks(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("restart forgot original completion authority: %v", err)
	}
	deleteTermination := `DELETE FROM action_lease_events WHERE action_id=? AND event_type='TERMINATED'`
	if _, err := successor.db.Exec(deleteTermination, input.ActionID); err == nil || !strings.Contains(err.Error(), "ACTION_LEASE_EVENT_IMMUTABLE") {
		t.Fatalf("termination journal was mutable: %v", err)
	}
	if _, _, receipt, found, err := successor.AgentNodeOutcomeSnapshot(input.ActionID, "coder"); err != nil || !found || receipt == nil {
		t.Fatalf("refused deletion damaged retained proof: %v", err)
	}
	// This fixture uses a disposable test database. Simulate offline corruption
	// inside one transaction, restoring the exact production trigger before commit.
	corruption, err := successor.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer corruption.Rollback()
	if _, err := corruption.Exec("DROP TRIGGER action_lease_events_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := corruption.Exec(deleteTermination, input.ActionID); err != nil {
		t.Fatal(err)
	}
	if _, err := corruption.Exec(actionAdmissionSchemaObjects["action_lease_events_no_delete"]); err != nil {
		t.Fatal(err)
	}
	if err := corruption.Commit(); err != nil {
		t.Fatal(err)
	}
	if _, _, _, _, err := successor.AgentNodeOutcomeSnapshot(input.ActionID, "coder"); err == nil {
		t.Fatal("missing termination history was accepted as completion")
	}
}

func TestTasksCompletionRefusesIncompleteFailedAndReboundAuthority(t *testing.T) {
	for _, name := range []string{"incomplete", "failed", "wrong-cursor", "wrong-owner", "wrong-claim", "wrong-epoch", "wrong-run"} {
		t.Run(name, func(t *testing.T) {
			var control *Control
			var input CompleteAgentTasksRequest
			if name == "incomplete" {
				var task AgentTaskSettlementRequest
				control, task, _ = taskSettlementFixture(t, false)
				input = CompleteAgentTasksRequest{OperationID: "complete-incomplete", Actor: task.Actor, ActionID: task.ActionID, Epoch: task.Epoch, RunID: task.RunID, Owner: task.Owner, ClaimToken: task.ClaimToken, ExpectedIndex: 4}
			} else {
				control, input = tasksCompletionFixture(t, name == "failed")
			}
			switch name {
			case "wrong-cursor":
				input.ExpectedIndex--
			case "wrong-owner":
				input.Owner = "other-owner"
			case "wrong-claim":
				input.ClaimToken = "other-claim"
			case "wrong-epoch":
				input.Epoch++
			case "wrong-run":
				input.RunID = "other-run"
			}
			before := countControlEvents(t, control)
			if _, err := control.CompleteAgentTasks(input); err == nil {
				t.Fatal("unverified phase completed")
			}
			if countControlEvents(t, control) != before {
				t.Fatal("refusal retained a partial completion")
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatal("refusal released unresolved resources")
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatal("refusal retained an operator receipt")
			}
		})
	}
}

func TestTasksCompletionSQLFailuresRollBackActionLeaseResourcesAndReceipt(t *testing.T) {
	for _, name := range []string{"abort-action", "ignore-action", "abort-termination", "ignore-termination", "ignore-lease", "ignore-release"} {
		t.Run(name, func(t *testing.T) {
			control, input := tasksCompletionFixture(t, false)
			before := countControlEvents(t, control)
			table, operation, condition, mode := "action_journal", "UPDATE", "", "ABORT,'injected tasks completion failure'"
			switch name {
			case "ignore-action":
				mode = "IGNORE"
			case "abort-termination":
				table, operation, condition = "action_lease_events", "INSERT", "WHEN NEW.event_type='TERMINATED'"
			case "ignore-termination":
				table, operation, condition, mode = "action_lease_events", "INSERT", "WHEN NEW.event_type='TERMINATED'", "IGNORE"
			case "ignore-lease":
				table, mode = "action_leases", "IGNORE"
			case "ignore-release":
				table, operation, mode = "action_resource_leases", "DELETE", "IGNORE"
			}
			query := fmt.Sprintf("CREATE TEMP TRIGGER completion_fault BEFORE %s ON %s %s BEGIN SELECT RAISE(%s); END", operation, table, condition, mode)
			if _, err := control.db.Exec(query); err != nil {
				t.Fatal(err)
			}
			if _, err := control.CompleteAgentTasks(input); err == nil {
				t.Fatal("lost SQL write was accepted")
			}
			if _, err := control.db.Exec("DROP TRIGGER completion_fault"); err != nil {
				t.Fatal(err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 || countControlEvents(t, control) != before {
				t.Fatal("failed transaction lost original phase")
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatal("failed transaction retained completion receipt")
			}
			if _, err := control.CompleteAgentTasks(input); err != nil {
				t.Fatalf("rollback poisoned valid retry: %v", err)
			}
		})
	}
}

func TestTasksCompletionCommitDeadlineCannotReleaseResources(t *testing.T) {
	for _, name := range []string{"writer", "action"} {
		t.Run(name, func(t *testing.T) {
			control, input := tasksCompletionFixture(t, false)
			clock := control.now
			start := clock()
			lease, found, err := control.GetActionLease(input.ActionID)
			if err != nil || !found {
				t.Fatal(err)
			}
			deadline, expected := start+control.leaseSeconds, ErrWriterFenceHeld
			if name == "action" {
				deadline, expected = lease.LeaseUntil, ErrActionLeaseExpired
				control.leaseSeconds = deadline - start + 120
			}
			before, calls := countControlEvents(t, control), 0
			control.now = func() int64 {
				calls++
				if calls == 1 {
					return start
				}
				return deadline
			}
			_, err = control.CompleteAgentTasks(input)
			control.now = clock
			if !errors.Is(err, expected) || countControlEvents(t, control) != before {
				t.Fatalf("expired %s commit retained phase completion: %v", name, err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatal("expired completion released resources")
			}
			if _, err := control.CompleteAgentTasks(input); err != nil {
				t.Fatalf("expired commit poisoned retry: %v", err)
			}
		})
	}
}
