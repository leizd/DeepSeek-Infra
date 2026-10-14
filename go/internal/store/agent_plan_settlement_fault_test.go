package store

import (
	"bytes"
	"errors"
	"math"
	"reflect"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
)

func assertPlanSettlementUnchanged(t *testing.T, control *Control, input AgentPlanSettlementRequest, before Record, events int, locks []ActionResourceLease) {
	t.Helper()
	after, found, err := control.Get("action", input.ActionID)
	current, lockErr := control.GetResourceLeases(input.ActionID)
	run, runFound, runErr := control.AgentRunMetadata(input.RunID)
	if err != nil || !found || lockErr != nil || runErr != nil || !runFound || after.Revision != before.Revision ||
		!bytes.Equal(after.Payload, before.Payload) || countControlEvents(t, control) != events || !reflect.DeepEqual(current, locks) || run.Status != "planning" || run.NextIndex != 2 {
		t.Fatalf("refusal retained a partial settlement or lost reservations: %v %v %v", err, lockErr, runErr)
	}
	if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
		t.Fatalf("refused settlement retained replay state: %v", err)
	}
}

func TestAgentPlanSettlementInvalidEnvelopeCannotMutate(t *testing.T) {
	for _, name := range []string{"action", "run", "zero-epoch", "overflow-epoch", "nil-proof", "invalid-utf8", "oversized-proof", "actor", "operation", "wrong-run", "wrong-epoch"} {
		t.Run(name, func(t *testing.T) {
			control, input, _ := planSettlementFixture(t, false)
			original := input
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			locks, err := control.GetResourceLeases(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			events := countControlEvents(t, control)
			switch name {
			case "action":
				input.ActionID = "../action"
			case "run":
				input.RunID = "../run"
			case "zero-epoch":
				input.Epoch = 0
			case "overflow-epoch":
				input.Epoch = math.MaxUint64
			case "nil-proof":
				input.Projection = nil
			case "invalid-utf8":
				input.Projection.Evidence.NormalizedPlan[0].Task = string([]byte{255})
			case "oversized-proof":
				input.Projection.Signature = make([]byte, maximumPayloadBytes+1)
			case "actor":
				input.Actor = ""
			case "operation":
				input.OperationID = ""
			case "wrong-run":
				input.RunID = "another_run"
			case "wrong-epoch":
				input.Epoch++
			}
			if _, err := control.SettleAgentPlan(input); err == nil {
				t.Fatal("invalid envelope settled a phase")
			}
			assertPlanSettlementUnchanged(t, control, original, before, events, locks)
		})
	}
}

func TestAgentPlanSettlementEveryDurableWriteRollsBackAndAllowsRetry(t *testing.T) {
	for _, table := range []string{"agent_runs", "control_events", "action_journal", "action_leases", "action_resource_leases", "action_lease_events", "control_operator_mutations", "deferred-commit"} {
		t.Run(table, func(t *testing.T) {
			control, input, _ := planSettlementFixture(t, false)
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			locks, err := control.GetResourceLeases(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			events := countControlEvents(t, control)
			statement := "CREATE TEMP TRIGGER plan_write_failure BEFORE INSERT ON " + table + " BEGIN SELECT RAISE(ABORT,'injected settlement failure'); END"
			switch table {
			case "agent_runs", "action_journal", "action_leases":
				statement = "CREATE TEMP TRIGGER plan_write_failure BEFORE UPDATE ON " + table + " BEGIN SELECT RAISE(ABORT,'injected settlement failure'); END"
			case "action_resource_leases":
				statement = "CREATE TEMP TRIGGER plan_write_failure BEFORE DELETE ON action_resource_leases BEGIN SELECT RAISE(ABORT,'injected settlement failure'); END"
			case "deferred-commit":
				for _, sql := range []string{"CREATE TEMP TABLE plan_parent(id INTEGER PRIMARY KEY)", "CREATE TEMP TABLE plan_child(id INTEGER REFERENCES plan_parent(id) DEFERRABLE INITIALLY DEFERRED)"} {
					if _, err := control.db.Exec(sql); err != nil {
						t.Fatal(err)
					}
				}
				statement = "CREATE TEMP TRIGGER plan_write_failure AFTER INSERT ON control_operator_mutations BEGIN INSERT INTO plan_child VALUES(1); END"
			}
			if _, err := control.db.Exec(statement); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentPlan(input); err == nil {
				t.Fatal("injected durable failure committed")
			}
			if _, err := control.db.Exec("DROP TRIGGER temp.plan_write_failure"); err != nil {
				t.Fatal(err)
			}
			assertPlanSettlementUnchanged(t, control, input, before, events, locks)
			if result, err := control.SettleAgentPlan(input); err != nil || result.AlreadyApplied {
				t.Fatalf("rollback poisoned a verified retry: %v", err)
			}
		})
	}
}

func TestAgentPlanSettlementRejectsAuthorityLossBeforeAndAtCommit(t *testing.T) {
	for _, name := range []string{"action-cutover-read", "action-unowned", "missing-action", "missing-lease", "missing-resources", "expired-action", "backward-lease", "commit-backward", "commit-writer-expired", "commit-action-expired", "commit-writer-replaced"} {
		t.Run(name, func(t *testing.T) {
			control, input, _ := planSettlementFixture(t, false)
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			locks, err := control.GetResourceLeases(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			lease, _, err := control.GetActionLease(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			events, token, clock := countControlEvents(t, control), control.token, control.now
			var view, statement string
			switch name {
			case "action-cutover-read":
				view = "control_cutover"
				statement = "CREATE TEMP VIEW control_cutover AS SELECT * FROM main.control_cutover WHERE domain<>'action'"
			case "action-unowned":
				view = "control_cutover"
				statement = "CREATE TEMP VIEW control_cutover AS SELECT domain, CASE WHEN domain='action' THEN 'shadow' ELSE state END AS state, revision, epoch, fencing_token, previous_owner, owner, transfer_id, writer_fencing_token, updated_at FROM main.control_cutover"
			case "missing-action":
				view = "action_journal"
				statement = "CREATE TEMP VIEW action_journal AS SELECT * FROM main.action_journal WHERE 0"
			case "missing-lease":
				view = "action_leases"
				statement = "CREATE TEMP VIEW action_leases AS SELECT * FROM main.action_leases WHERE 0"
			case "missing-resources":
				view = "action_resource_leases"
				statement = "CREATE TEMP VIEW action_resource_leases AS SELECT * FROM main.action_resource_leases WHERE 0"
			case "expired-action":
				if _, err := control.db.Exec("UPDATE action_leases SET lease_until=? WHERE action_id=?", clock(), input.ActionID); err != nil {
					t.Fatal(err)
				}
			case "backward-lease":
				if _, err := control.db.Exec("UPDATE action_leases SET updated_at=? WHERE action_id=?", clock()+1, input.ActionID); err != nil {
					t.Fatal(err)
				}
			default:
				start, calls := clock(), 0
				control.now = func() int64 {
					calls++
					if calls == 1 {
						return start
					}
					switch name {
					case "commit-backward":
						return start - 1
					case "commit-writer-expired":
						return start + control.leaseSeconds
					case "commit-action-expired":
						return lease.LeaseUntil
					case "commit-writer-replaced":
						control.token++
					}
					return start
				}
				if name == "commit-action-expired" {
					// Keep writer authority live while the phase lease expires.
					if _, err := control.db.Exec("UPDATE control_writer SET lease_until=?", lease.LeaseUntil+control.leaseSeconds); err != nil {
						t.Fatal(err)
					}
				}
			}
			if statement != "" {
				if _, err := control.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.SettleAgentPlan(input); err == nil {
				t.Fatal("lost authority completed a phase")
			}
			control.now, control.token = clock, token
			if view != "" {
				if _, err := control.db.Exec("DROP VIEW temp." + view); err != nil {
					t.Fatal(err)
				}
			}
			if name == "expired-action" {
				if _, err := control.db.Exec("UPDATE action_leases SET lease_until=? WHERE action_id=?", lease.LeaseUntil, input.ActionID); err != nil {
					t.Fatal(err)
				}
			}
			if name == "backward-lease" {
				if _, err := control.db.Exec("UPDATE action_leases SET updated_at=? WHERE action_id=?", clock(), input.ActionID); err != nil {
					t.Fatal(err)
				}
			}
			assertPlanSettlementUnchanged(t, control, input, before, events, locks)
			if _, err := control.SettleAgentPlan(input); err != nil {
				t.Fatalf("authority refusal poisoned valid retry: %v", err)
			}
		})
	}
}

func TestAgentPlanSettlementRefusesChangedCursorAndOperationReplay(t *testing.T) {
	control, input, _ := planSettlementFixture(t, false)
	result, err := control.SettleAgentPlan(input)
	if err != nil {
		t.Fatal(err)
	}
	count := countControlEvents(t, control)
	input.Actor = "another-operator"
	if _, err := control.SettleAgentPlan(input); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("operation rebinding was accepted: %v", err)
	}
	if countControlEvents(t, control) != count {
		t.Fatal("conflicting replay wrote events")
	}
	input.OperationID = "second-settlement"
	if _, err := control.SettleAgentPlan(input); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("new operation repeated terminal settlement: %v", err)
	}
	if run, _, err := control.AgentRunMetadata(input.RunID); err != nil || run.NextIndex != result.Run.NextIndex {
		t.Fatal("replay changed settled cursor")
	}
	other, bad, _ := planSettlementFixture(t, false)
	mutation := eventMutation(t, other, 2, agent.RunEvent{Type: "agent_plan", Plan: []agent.RunPlanNode{{ID: "replacement", Task: "changed plan"}}})
	mutation.OperationID, mutation.RunID, mutation.Fence.ActionId = "change-before-settlement", bad.RunID, bad.RunID
	mutation.Create = nil
	if _, err := other.AppendAgentRunEvent(mutation); err != nil {
		t.Fatal(err)
	}
	if _, err := other.SettleAgentPlan(bad); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("stale phase consumed changed metadata: %v", err)
	}
	if resources, err := other.GetResourceLeases(bad.ActionID); err != nil || len(resources) != 1 {
		t.Fatal("stale phase released unresolved reservation")
	}
	if _, err := other.SettleAgentPlan(AgentPlanSettlementRequest{}); !errors.Is(err, agentproof.ErrAgentPlanProjectionInvalid) {
		t.Fatal("invalid envelope lost its stable error")
	}
}
