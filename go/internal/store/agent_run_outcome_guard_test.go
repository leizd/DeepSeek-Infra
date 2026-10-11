package store

import (
	"bytes"
	"errors"
	"fmt"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

func TestAgentGenericOutcomeEventsCannotSettleNativeReservation(t *testing.T) {
	for _, recorded := range []bool{false, true} {
		t.Run(fmt.Sprint(recorded), func(t *testing.T) {
			control, input, _ := agentOutcomeFixture(t)
			if recorded {
				if _, err := control.RecordAgentNodeOutcome(input); err != nil {
					t.Fatal(err)
				}
			}
			before, _, err := control.Get("agent_run", input.RunID)
			if err != nil {
				t.Fatal(err)
			}
			count := countControlEvents(t, control)
			for index, event := range []agent.RunEvent{
				{Type: "done"}, {Type: "run_status", Status: "done"},
				{Type: "agent", Phase: "coder", Status: "done"},
				{Type: "agent", Phase: "coder", Status: "error"},
				{Type: "agent_output", Phase: "coder"},
				{Type: "agent_output", Phase: "coder", Failed: true},
			} {
				mutation := eventMutation(t, control, before.Revision, event)
				mutation.Create = nil
				mutation.OperationID = fmt.Sprintf("unverified-public-outcome-%d", index)
				if _, err := control.AppendAgentRunEvent(mutation); !errors.Is(err, ErrAgentRunExecutionRequired) {
					t.Fatalf("generic outcome claims native settlement: %v", err)
				}
				if _, found, err := control.ReadOperatorMutation(mutation.OperationID); err != nil || found {
					t.Fatalf("refused outcome retained operation receipt: %v", err)
				}
			}
			after, found, err := control.Get("agent_run", input.RunID)
			if err != nil || !found || before.Revision != after.Revision || !bytes.Equal(before.Payload, after.Payload) || countControlEvents(t, control) != count {
				t.Fatalf("refused generic outcome changed public state/history: %v", err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatalf("unresolved effect lost its reservation: %v", err)
			}
		})
	}
}

func TestAgentNativeReservationPreservesHistoricalEventReplayAndCancellation(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	before := countControlEvents(t, control)
	replay := eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "created"})
	result, err := control.AppendAgentRunEvent(replay)
	if err != nil || result.Event.Index != 0 || result.Run.NextIndex != 4 || countControlEvents(t, control) != before {
		t.Fatalf("native reservation refused immutable event replay: %v", err)
	}
	cancel := eventMutation(t, control, 4, agent.RunEvent{Type: "run_status", Status: "cancelled"})
	cancel.Create = nil
	if _, err := control.AppendAgentRunEvent(cancel); err != nil {
		t.Fatalf("native reservation prevented user cancellation: %v", err)
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 1 {
		t.Fatalf("cancellation treated an unresolved effect as unapplied: %v", err)
	}
}

func TestAgentOutcomeReservationReadFailureRefusesPublicCommit(t *testing.T) {
	control, input := agentNodeDispatchFixture(t)
	count := countControlEvents(t, control)
	// A real SQLite namespace failure reaches the reservation read, rather than
	// mocking the guard or changing any authoritative durable table.
	if _, err := control.db.Exec(`CREATE TEMP VIEW action_resource_leases AS SELECT resource_key FROM missing_outcome_reservations`); err != nil {
		t.Fatal(err)
	}
	mutation := eventMutation(t, control, 4, agent.RunEvent{Type: "done"})
	mutation.Create = nil
	if _, err := control.AppendAgentRunEvent(mutation); err == nil {
		t.Fatal("reservation read failure committed public success")
	}
	if countControlEvents(t, control) != count {
		t.Fatal("reservation read failure retained partial public history")
	}
	if _, err := control.db.Exec(`DROP VIEW temp.action_resource_leases`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`CREATE TEMP VIEW action_resource_leases AS
		SELECT resource_key, 0 AS lease_until FROM main.action_resource_leases`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AppendAgentRunEvent(mutation); !errors.Is(err, ErrAgentRunExecutionRequired) {
		t.Fatalf("expired reservation was treated as absent effect evidence: %v", err)
	}
	if _, err := control.db.Exec(`DROP VIEW temp.action_resource_leases`); err != nil {
		t.Fatal(err)
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 1 {
		t.Fatalf("read failure changed unresolved resources: %v", err)
	}
}
