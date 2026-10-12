package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func TestAgentNodeOutcomeFaultsRetainActionAndReservations(t *testing.T) {
	for _, change := range []string{"invalid_utf8", "private_actor", "action_cutover_read", "action_history_read", "action_lease_read", "commit_writer", "deferred_commit"} {
		t.Run(change, func(t *testing.T) {
			control, input, _ := agentOutcomeFixture(t)
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			locks, err := control.GetResourceLeases(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			count, originalToken := countControlEvents(t, control), control.token
			var statements []string
			switch change {
			case "invalid_utf8":
				input.Receipt.Evidence.ProviderPlan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: string([]byte{255})}}
			case "private_actor":
				input.Actor = "operator:AGE-SECRET-KEY-1EXAMPLE"
			case "action_cutover_read":
				statements = []string{"CREATE TEMP VIEW control_cutover AS SELECT * FROM main.control_cutover WHERE domain<>'action'"}
			case "action_history_read":
				statements = []string{"CREATE TEMP VIEW control_events AS SELECT * FROM main.control_events WHERE domain<>'action'"}
			case "action_lease_read":
				statements = []string{"CREATE TEMP VIEW action_leases AS SELECT * FROM main.action_leases WHERE 0"}
			case "commit_writer":
				now, calls := control.now(), 0
				control.now = func() int64 {
					calls++
					if calls == 2 {
						control.token++
					}
					return now
				}
			case "deferred_commit":
				statements = []string{
					"CREATE TEMP TABLE agent_test_parent(id INTEGER PRIMARY KEY)",
					"CREATE TEMP TABLE agent_test_child(id INTEGER REFERENCES agent_test_parent(id) DEFERRABLE INITIALLY DEFERRED)",
					"CREATE TEMP TRIGGER agent_test_commit_failure AFTER INSERT ON control_operator_mutations BEGIN INSERT INTO agent_test_child VALUES(1); END",
				}
			}
			for _, statement := range statements {
				if _, err := control.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			_, rejected := control.RecordAgentNodeOutcome(input)
			control.token = originalToken
			if rejected == nil {
				t.Fatal("faulted receipt ingestion returned success")
			}
			if change == "action_history_read" {
				if _, err := control.db.Exec("DROP VIEW temp.control_events"); err != nil {
					t.Fatal(err)
				}
			}
			after, _, err := control.Get("action", input.ActionID)
			currentLocks, lockErr := control.GetResourceLeases(input.ActionID)
			if err != nil || lockErr != nil || after.Revision != before.Revision || !bytes.Equal(after.Payload, before.Payload) ||
				countControlEvents(t, control) != count || !reflect.DeepEqual(locks, currentLocks) {
				t.Fatalf("receipt rejection left partial history or lost resources: %v %v", err, lockErr)
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatalf("failed receipt ingestion became replayable: %v", err)
			}
		})
	}
}

func TestAgentNodeOutcomeSnapshotChecksProofBelowConsistentRecordDigests(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	if _, err := control.RecordAgentNodeOutcome(input); err != nil {
		t.Fatal(err)
	}
	record, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(record.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	values, err := agentNodeOutcomeBytes(record)
	if err != nil {
		t.Fatal(err)
	}
	value := values[input.NodeID]
	value.Receipt[len(value.Receipt)-1] ^= 1
	values[input.NodeID] = value
	fields[agentNodeOutcomeField], _ = json.Marshal(values)
	record.Payload, _ = json.Marshal(fields)
	record.Payload, err = canonicalControlPayload(record.Payload)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := protocol.Digest(record)
	if err != nil {
		t.Fatal(err)
	}
	quote := func(text string) string { return "'" + strings.ReplaceAll(text, "'", "''") + "'" }
	payload, recordDigest := quote(string(record.Payload)), quote(digest)
	statements := []string{
		fmt.Sprintf(`CREATE TEMP VIEW action_journal AS SELECT id, revision, execution_epoch, state,
			CASE WHEN id=%s THEN %s ELSE payload_json END AS payload_json,
			CASE WHEN id=%s THEN %s ELSE record_digest END AS record_digest,
			writer_fencing_token, updated_at FROM main.action_journal`, quote(record.ID), payload, quote(record.ID), recordDigest),
		fmt.Sprintf(`CREATE TEMP VIEW control_events AS SELECT event_id, domain, record_id, revision, execution_epoch, state,
			CASE WHEN domain='action' AND record_id=%s AND revision=%d THEN %s ELSE payload_json END AS payload_json,
			CASE WHEN domain='action' AND record_id=%s AND revision=%d THEN %s ELSE record_digest END AS record_digest,
			writer_fencing_token, recorded_at FROM main.control_events`, quote(record.ID), record.Revision, payload,
			quote(record.ID), record.Revision, recordDigest),
	}
	for _, statement := range statements {
		if _, err := control.db.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if _, found, err := control.Get("action", record.ID); err != nil || !found {
		t.Fatalf("fixture did not reach cryptographic evidence verification: %v", err)
	}
	if _, _, _, _, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("consistent record digests hid an invalid signed proof: %v", err)
	}
	if _, err := control.RecordAgentNodeOutcome(input); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt stored evidence acquired a successful replay: %v", err)
	}
}

func TestAgentNodeOutcomeSnapshotUnavailableDatabaseAndAbsentNode(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	if _, _, receipt, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, "absent"); err != nil || found || receipt != nil {
		t.Fatalf("unknown node acquired historical evidence: %v", err)
	}
	if err := control.db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, _, _, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID); err == nil {
		t.Fatal("database unavailability was reported as a successful snapshot")
	}
}

func TestAgentLeaderDispatchRequiresTheOriginalPlanPhase(t *testing.T) {
	control := agentExecutionControl(t, true)
	for index, status := range []string{"created", "planning"} {
		mutation := eventMutation(t, control, int64(index), agent.RunEvent{Type: "run_status", Status: status})
		mutation.OperationID = fmt.Sprintf("leader-event-%d", index)
		mutation.RunID, mutation.Fence.ActionId = "run_leader", "run_leader"
		if index != 0 {
			mutation.Create = nil
		}
		if _, err := control.AppendAgentRunEvent(mutation); err != nil {
			t.Fatal(err)
		}
	}
	request := agentExecutionRequest(t, control)
	request.OperationID, request.RunID, request.Fence.ActionId = "leader-phase-claim", "run_leader", "run_leader"
	request.Phase, request.ExpectedIndex = "plan", 2
	claim, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	lease := claim.Admission.Lease
	input := AgentNodeDispatchRequest{OperationID: "leader-node-begin", Actor: request.Actor, ActionID: lease.ActionID,
		Epoch: lease.Epoch, RunID: request.RunID, NodeID: "leader", Owner: lease.Owner, ClaimToken: lease.ClaimToken}
	first, err := control.BeginAgentNodeDispatch(input)
	if err != nil || !first.Fresh || first.Intent.Phase != "plan" || first.Intent.NodeID != "leader" || agentOutcomeIntent(first.Intent).Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN {
		t.Fatalf("leader was not bound to the original plan phase: %v", err)
	}
	if replay, err := control.BeginAgentNodeDispatch(input); err != nil || replay.Fresh || !reflect.DeepEqual(replay.Intent, first.Intent) {
		t.Fatalf("leader replay authorized a duplicate provider effect: %v", err)
	}
}
