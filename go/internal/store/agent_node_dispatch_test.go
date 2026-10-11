package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

func agentNodeDispatchFixture(t *testing.T) (*Control, AgentNodeDispatchRequest) {
	t.Helper()
	control := agentExecutionControl(t, true)
	phase, err := control.ClaimAgentRunExecution(agentExecutionRequest(t, control))
	if err != nil {
		t.Fatal(err)
	}
	lease := phase.Admission.Lease
	return control, AgentNodeDispatchRequest{OperationID: "node-coder-begin", Actor: "operator:agent-test",
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: "run_events", NodeID: "coder",
		Owner: lease.Owner, ClaimToken: lease.ClaimToken}
}

func TestAgentNodeDispatchKeepsGenericActionChannelFenced(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	authority, err := control.GetCutover("action")
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.BeginAgentNodeDispatch(input)
	if err != nil || !result.Fresh || result.Mutation.Domain != "action" ||
		result.Mutation.RecordID != input.ActionID || result.Mutation.CutoverRevision != authority.Revision ||
		result.Mutation.FencingToken != authority.FencingToken {
		t.Fatalf("typed Agent dispatch lost action authority provenance: %+v %v", result, err)
	}
	before := countControlEvents(t, control)
	_, err = control.ApplyOperatorMutation(OperatorMutation{OperationID: "generic-agent-action-write", ActionID: input.ActionID,
		Actor: input.Actor, Record: result.Admission.Record})
	if !errors.Is(err, ErrMutationRequestDomainFenced) || countControlEvents(t, control) != before {
		t.Fatalf("typed Agent admission opened the generic action write channel: %v", err)
	}
}

func TestAgentNodeDispatchDeniesEitherLostAuthorityWithoutPartialIntent(t *testing.T) {
	for _, domain := range []string{"agent_run", "action"} {
		t.Run(domain, func(t *testing.T) {
			control, input := agentNodeDispatchFixture(t)
			if _, err := control.db.Exec("UPDATE control_cutover SET state=? WHERE domain=?", CutoverShadow, domain); err != nil {
				t.Fatal(err)
			}
			before := countControlEvents(t, control)
			if _, err := control.BeginAgentNodeDispatch(input); !errors.Is(err, ErrCutoverNotAuthorized) {
				t.Fatalf("lost %s authority still admitted a provider dispatch: %v", domain, err)
			}
			if countControlEvents(t, control) != before {
				t.Fatal("refused dispatch wrote action history")
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatalf("refused dispatch retained an applied receipt: %v %v", found, err)
			}
			if _, found, err := control.AgentNodeDispatch(input.ActionID, input.NodeID); err != nil || found {
				t.Fatalf("refused dispatch retained a partial intent: %v %v", found, err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatalf("refused dispatch released the live phase resource: %+v %v", resources, err)
			}
		})
	}
}

func TestAgentNodeDispatchLostResponseReplayIsNeverFresh(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	count := countControlEvents(t, control)
	first, err := control.BeginAgentNodeDispatch(input)
	if err != nil || !first.Fresh || first.Intent.State != "INTENT_RECORDED" ||
		first.Intent.ActionID != input.ActionID || first.Intent.ExecutionEpoch != input.Epoch ||
		first.Admission.Record.State != "EXECUTING" || first.Admission.Lease.ClaimToken != input.ClaimToken {
		t.Fatalf("unbound fresh control intent: %+v %v", first, err)
	}
	if countControlEvents(t, control) != count+1 {
		t.Fatal("the durable dispatch did not commit exactly one action history event")
	}
	second, err := control.BeginAgentNodeDispatch(input)
	if err != nil || second.Fresh || second.Intent.OperationID != first.Intent.OperationID ||
		second.Mutation.Status != OperatorMutationAlreadyApplied || countControlEvents(t, control) != count+1 {
		t.Fatalf("lost-response retry admitted another dispatch: %+v %v", second, err)
	}
	input.OperationID = "node-coder-second-operation"
	third, err := control.BeginAgentNodeDispatch(input)
	if err != nil || third.Fresh || third.Intent.OperationID != first.Intent.OperationID || countControlEvents(t, control) != count+1 {
		t.Fatalf("another operation ID bypassed the existing intent: %+v %v", third, err)
	}
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found || run.Nodes()["coder"].Attempts != 0 || run.NextIndex != 4 {
		t.Fatalf("a control intent fabricated provider/node progress: %+v %v", run, err)
	}
}

func TestAgentNodeDispatchConcurrentBeginsHaveOneFreshWinner(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	results := make(chan AgentNodeDispatchResult, 12)
	errors := make(chan error, 12)
	var workers sync.WaitGroup
	for index := 0; index < 12; index++ {
		workers.Add(1)
		go func(index int) {
			defer workers.Done()
			request := input
			request.OperationID = fmt.Sprintf("node-concurrent-%d", index)
			result, err := control.BeginAgentNodeDispatch(request)
			results <- result
			errors <- err
		}(index)
	}
	workers.Wait()
	close(results)
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	fresh := 0
	operationID := ""
	for result := range results {
		if result.Fresh {
			fresh++
		}
		if operationID == "" {
			operationID = result.Intent.OperationID
		} else if operationID != result.Intent.OperationID {
			t.Fatal("concurrent control intents diverged")
		}
	}
	if fresh != 1 {
		t.Fatalf("fresh dispatch decisions = %d, want exactly one", fresh)
	}
}

func TestAgentNodeDispatchRejectsProjectedDependencyCompletion(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	projection := eventMutation(t, control, 4, agent.RunEvent{Type: "agent_output", Phase: "coder"})
	projection.Create = nil
	if _, err := control.AppendAgentRunEvent(projection); err != nil {
		t.Fatal(err)
	}
	input.NodeID, input.OperationID = "critic", "node-critic-begin"
	if _, err := control.BeginAgentNodeDispatch(input); !errors.Is(err, ErrAgentNodeDependencyUnverified) {
		t.Fatalf("imported output metadata granted a provider-dependent node: %v", err)
	}
}

func TestAgentNodeDispatchUnknownPhaseAndGenericWritesRetainIntent(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	first, err := control.BeginAgentNodeDispatch(input)
	if err != nil {
		t.Fatal(err)
	}
	record, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(record.Payload, &payload); err != nil {
		t.Fatal(err)
	}
	delete(payload, agentNodeDispatchField)
	raw, _ := json.Marshal(payload)
	if _, err := control.CompleteAction(input.ActionID, input.Epoch, input.ClaimToken, raw); !errors.Is(err, ErrAgentNodeDispatchRequired) {
		t.Fatalf("generic completion erased an unresolved node intent: %v", err)
	}
	if _, err := control.MarkActionEffectUnknown(input.ActionID, input.Epoch, input.ClaimToken); err != nil {
		t.Fatal(err)
	}
	input.OperationID = "node-coder-after-unknown"
	if _, err := control.BeginAgentNodeDispatch(input); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("unknown phase permitted a fresh effect: %v", err)
	}
	retained, found, err := control.AgentNodeDispatch(input.ActionID, input.NodeID)
	if err != nil || !found || retained.OperationID != first.Intent.OperationID || retained.State != "INTENT_RECORDED" {
		t.Fatalf("unknown provider outcome lost its immutable intent: %+v %v", retained, err)
	}
}

func TestAgentNodeDispatchGenericTerminalCannotReleaseUnverifiedResources(t *testing.T) {
	for _, terminal := range []string{"success", "failure-before-effect"} {
		t.Run(terminal, func(t *testing.T) {
			control, input := agentNodeDispatchFixture(t)
			first, err := control.BeginAgentNodeDispatch(input)
			if err != nil {
				t.Fatal(err)
			}
			count := countControlEvents(t, control)
			var result Record
			if terminal == "success" {
				result, err = control.CompleteAction(input.ActionID, input.Epoch, input.ClaimToken, first.Admission.Record.Payload)
			} else {
				result, err = control.FailAction(input.ActionID, input.Epoch, input.ClaimToken, first.Admission.Record.Payload)
			}
			if !errors.Is(err, ErrAgentNodeDispatchRequired) || result.ID != "" {
				t.Fatalf("unverified %s released the phase: %+v %v", terminal, result, err)
			}
			record, found, err := control.Get("action", input.ActionID)
			if err != nil || !found || record.State != "EXECUTING" ||
				record.Revision != first.Admission.Record.Revision || !bytes.Equal(record.Payload, first.Admission.Record.Payload) {
				t.Fatalf("refused terminal operation changed the action: %+v %v", record, err)
			}
			lease, found, err := control.GetActionLease(input.ActionID)
			if err != nil || !found || lease.ClaimToken != input.ClaimToken || countControlEvents(t, control) != count {
				t.Fatalf("refused terminal operation lost its lease or wrote history: %+v %v", lease, err)
			}
			if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: input.ActionID, Epoch: input.Epoch,
				Owner: input.Owner, ClaimToken: input.ClaimToken, LeaseSeconds: 60}); err != nil {
				t.Fatalf("the refused completion lost the run resource reservation: %v", err)
			}
		})
	}
}

func TestAgentNodeDispatchIdentityIgnoresObjectOrderAndRejectsAlteration(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	first, err := control.BeginAgentNodeDispatch(input)
	if err != nil {
		t.Fatal(err)
	}
	record := first.Admission.Record
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(record.Payload, &payload); err != nil {
		t.Fatal(err)
	}
	nodes, err := readAgentNodeDispatches(record)
	if err != nil {
		t.Fatal(err)
	}
	// Typed struct order differs from the canonical object's sorted keys.
	payload[agentNodeDispatchField], err = json.Marshal(nodes)
	if err != nil {
		t.Fatal(err)
	}
	reordered := record
	reordered.Payload, _ = json.Marshal(payload)
	if !sameAgentNodeDispatches(record, reordered) {
		t.Fatal("JSON object order changed the immutable dispatch identity")
	}
	changed := nodes[input.NodeID]
	changed.Task = "a different provider request"
	changed.OperationID = agentNodeOperationID(changed)
	nodes[input.NodeID] = changed
	payload[agentNodeDispatchField], _ = json.Marshal(nodes)
	reordered.Payload, _ = json.Marshal(payload)
	if sameAgentNodeDispatches(record, reordered) {
		t.Fatal("another task with a recomputed operation ID replaced the committed intent")
	}
}

func TestAgentNodeDispatchRefusesChangedIdentityCancellationAndExpiry(t *testing.T) {
	for _, scenario := range []string{"epoch", "owner", "token", "run", "node", "cancelled", "expired"} {
		t.Run(scenario, func(t *testing.T) {
			control, input := agentNodeDispatchFixture(t)
			switch scenario {
			case "epoch":
				input.Epoch++
			case "owner":
				input.Owner = "another-worker"
			case "token":
				input.ClaimToken = "another-claim-token"
			case "run":
				input.RunID = "another-run"
			case "node":
				input.NodeID = "synthesizer"
			case "cancelled":
				event := eventMutation(t, control, 4, agent.RunEvent{Type: "run_status", Status: "cancelled"})
				event.Create = nil
				if _, err := control.AppendAgentRunEvent(event); err != nil {
					t.Fatal(err)
				}
			case "expired":
				lease, found, err := control.GetActionLease(input.ActionID)
				if err != nil || !found {
					t.Fatalf("missing fixture claim: %v", err)
				}
				control.now = func() int64 { return lease.LeaseUntil + 1 }
			}
			count := countControlEvents(t, control)
			if result, err := control.BeginAgentNodeDispatch(input); err == nil || result.Fresh {
				t.Fatalf("%s admitted a node: %+v %v", scenario, result, err)
			}
			if countControlEvents(t, control) != count {
				t.Fatal("refusal wrote an action history event")
			}
		})
	}
}

func TestAgentNodeDispatchRejectsCorruptHistoryBindings(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	first, err := control.BeginAgentNodeDispatch(input)
	if err != nil {
		t.Fatal(err)
	}
	for _, scenario := range []string{"schema", "action", "epoch", "run", "phase", "request", "plan", "index", "metadata-epoch", "state", "time", "operation", "unknown-field", "null-map"} {
		t.Run(scenario, func(t *testing.T) {
			record := first.Admission.Record
			var payload map[string]json.RawMessage
			if err := json.Unmarshal(record.Payload, &payload); err != nil {
				t.Fatal(err)
			}
			node := first.Intent
			switch scenario {
			case "schema":
				node.SchemaVersion++
			case "action":
				node.ActionID = "other-action"
			case "epoch":
				node.ExecutionEpoch++
			case "run":
				node.RunID = "other-run"
			case "phase":
				node.Phase = "plan"
			case "request":
				node.Request.Length++
			case "plan":
				node.PlanDigest = "wrong"
			case "index":
				node.MetadataIndex++
			case "metadata-epoch":
				node.MetadataEpoch++
			case "state":
				node.State = "CONFIRMED"
			case "time":
				node.RecordedAt = -1
			case "operation":
				node.OperationID = "wrong"
			}
			payload[agentNodeDispatchField], _ = json.Marshal(map[string]AgentNodeDispatch{input.NodeID: node})
			if scenario == "unknown-field" {
				payload[agentNodeDispatchField] = json.RawMessage(`{"coder":{"unknown":true}}`)
			}
			if scenario == "null-map" {
				payload[agentNodeDispatchField] = json.RawMessage(`null`)
			}
			record.Payload, _ = json.Marshal(payload)
			if _, err := readAgentNodeDispatches(record); !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("corrupt %s accepted: %v", scenario, err)
			}
			if sameAgentNodeDispatches(first.Admission.Record, record) {
				t.Fatal("corrupt dispatch treated as immutable identity")
			}
		})
	}
	if nativeAgentNodeDispatches(json.RawMessage(`invalid`)) {
		t.Fatal("invalid payload marked as a dispatch record")
	}
	if _, err := readAgentNodeDispatches(Record{Payload: json.RawMessage(`invalid`)}); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("invalid payload accepted: %v", err)
	}
	if _, found, err := control.AgentNodeDispatch("missing", input.NodeID); err != nil || found {
		t.Fatalf("absent action invented a control intent: %v %v", found, err)
	}
	conflict := input
	conflict.Actor = "operator:other-actor"
	if _, err := control.BeginAgentNodeDispatch(conflict); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("changed replay identity accepted: %v", err)
	}
}

func TestAgentNodeDispatchRefusesBrokenRunLeaseAndResourceEvidence(t *testing.T) {
	for _, scenario := range []string{"missing-action-lease", "expired-action-lease", "missing-resource", "missing-run", "metadata-epoch", "missing-plan-node", "invalid-node-input", "missing-operation", "missing-actor"} {
		t.Run(scenario, func(t *testing.T) {
			control, input := agentNodeDispatchFixture(t)
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			switch scenario {
			case "missing-action-lease":
				_, err = control.db.Exec("DELETE FROM action_leases WHERE action_id=?", input.ActionID)
			case "expired-action-lease":
				_, err = control.db.Exec("UPDATE action_leases SET lease_until=? WHERE action_id=?", control.now(), input.ActionID)
			case "missing-resource":
				_, err = control.db.Exec("DELETE FROM resource_leases WHERE action_id=?", input.ActionID)
			case "missing-run":
				_, err = control.db.Exec("DELETE FROM agent_runs WHERE id=?", input.RunID)
			case "metadata-epoch":
				_, err = control.db.Exec("UPDATE control_cutover SET epoch=epoch+1 WHERE domain='agent_run'")
			case "missing-plan-node":
				input.NodeID = "not-in-approved-plan"
			case "invalid-node-input":
				input.NodeID = ""
			case "missing-operation":
				input.OperationID = ""
			case "missing-actor":
				input.Actor = ""
			}
			if err != nil {
				t.Fatal(err)
			}
			count := countControlEvents(t, control)
			if _, err := control.BeginAgentNodeDispatch(input); err == nil {
				t.Fatalf("broken %s evidence admitted a dispatch", scenario)
			}
			after, found, err := control.Get("action", input.ActionID)
			if err != nil || !found || after.Revision != before.Revision || after.State != before.State ||
				!bytes.Equal(after.Payload, before.Payload) || countControlEvents(t, control) != count {
				t.Fatalf("refused %s changed the action or its history: %+v %v", scenario, after, err)
			}
			if input.OperationID != "" {
				if _, exists, err := control.ReadOperatorMutation(input.OperationID); err != nil || exists {
					t.Fatalf("refused %s left an applied receipt: %v %v", scenario, exists, err)
				}
			}
		})
	}
}

func TestAgentNodeDispatchSQLiteWriteFailuresRollbackIntentHistoryAndReceipt(t *testing.T) {
	for _, table := range []string{"action_journal", "control_events", "control_operator_mutations"} {
		t.Run(table, func(t *testing.T) {
			control, input := agentNodeDispatchFixture(t)
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			verb := "INSERT"
			if table == "action_journal" {
				verb = "UPDATE"
			}
			trigger := "CREATE TRIGGER agent_node_test_write_failure BEFORE " + verb + " ON " + table +
				" BEGIN SELECT RAISE(ABORT,'injected Agent node write failure'); END"
			if _, err := control.db.Exec(trigger); err != nil {
				t.Fatal(err)
			}
			count := countControlEvents(t, control)
			if _, err := control.BeginAgentNodeDispatch(input); err == nil {
				t.Fatal("failed SQLite write still returned a fresh provider permission")
			}
			after, found, err := control.Get("action", input.ActionID)
			if err != nil || !found || after.Revision != before.Revision || after.State != before.State ||
				!bytes.Equal(after.Payload, before.Payload) || countControlEvents(t, control) != count {
				t.Fatalf("failed %s write retained partial action state/history: %+v %v", table, after, err)
			}
			if _, exists, err := control.ReadOperatorMutation(input.OperationID); err != nil || exists {
				t.Fatalf("failed %s write left an applied receipt: %v %v", table, exists, err)
			}
			if _, err := control.db.Exec("DROP TRIGGER agent_node_test_write_failure"); err != nil {
				t.Fatal(err)
			}
			if result, err := control.BeginAgentNodeDispatch(input); err != nil || !result.Fresh {
				t.Fatalf("rollback left a poisoned reservation: %+v %v", result, err)
			}
		})
	}
}

func TestAgentNodeDispatchCommitClockLossRollsBackWithoutReleasingResources(t *testing.T) {
	for _, scenario := range []string{"expired", "backward"} {
		t.Run(scenario, func(t *testing.T) {
			control, input := agentNodeDispatchFixture(t)
			before := countControlEvents(t, control)
			lease, found, err := control.GetActionLease(input.ActionID)
			if err != nil || !found {
				t.Fatalf("missing fixture lease: %v", err)
			}
			originalClock := control.now
			start := originalClock()
			calls := 0
			control.now = func() int64 {
				calls++
				if calls == 1 {
					return start
				}
				if scenario == "expired" {
					return lease.LeaseUntil
				}
				return start - 1
			}
			_, err = control.BeginAgentNodeDispatch(input)
			control.now = originalClock
			if !errors.Is(err, ErrActionLeaseExpired) || calls < 2 || countControlEvents(t, control) != before {
				t.Fatalf("commit %s accepted a stale dispatch or partial history: %v", scenario, err)
			}
			if _, exists, err := control.ReadOperatorMutation(input.OperationID); err != nil || exists {
				t.Fatalf("commit clock loss retained an applied receipt: %v %v", exists, err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatalf("commit clock loss released its unresolved phase resource: %+v %v", resources, err)
			}
		})
	}
}

func TestAgentNodeDispatchMalformedBindingAndClosedSnapshotAreExplicitErrors(t *testing.T) {
	if _, err := readAgentNodeDispatches(Record{Payload: json.RawMessage(`{}`)}); !errors.Is(err, ErrAgentRunExecutionRequired) {
		t.Fatalf("unbound payload silently became an empty intent map: %v", err)
	}
	control, input := agentNodeDispatchFixture(t)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, found, err := control.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID); err == nil || found {
		t.Fatalf("closed storage silently looked like an absent effect: %v %v", found, err)
	}
}
