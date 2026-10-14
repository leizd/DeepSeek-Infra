package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func alterPlanSettlementReceipt(t *testing.T, record Record, alter func(*agentv1.AgentPlanProjectionReceipt)) Record {
	t.Helper()
	var fields map[string]json.RawMessage
	var encoded []byte
	var receipt agentv1.AgentPlanProjectionReceipt
	if err := json.Unmarshal(record.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(fields[agentPlanSettlementField], &encoded); err != nil {
		t.Fatal(err)
	}
	if err := proto.Unmarshal(encoded, &receipt); err != nil {
		t.Fatal(err)
	}
	alter(&receipt)
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
	if err != nil {
		t.Fatal(err)
	}
	fields[agentPlanSettlementField], err = json.Marshal(encoded)
	if err != nil {
		t.Fatal(err)
	}
	record.Payload, err = json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return record
}

func TestAgentPlanSettlementHistoryEdgeCannotWidenTheWriterBoundary(t *testing.T) {
	control, input, _ := planSettlementFixture(t, false)
	before, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.SettleAgentPlan(input)
	if err != nil {
		t.Fatal(err)
	}
	if !nativeAgentPlanSettlementEdge(before, result.Action) {
		t.Fatal("verified terminal edge cannot be replayed")
	}
	for _, name := range []string{"domain", "id", "epoch", "revision", "state", "before-json", "after-json", "existing-marker", "missing-marker", "invalid-marker-json", "invalid-protobuf", "schema", "missing-evidence", "missing-fence", "foreign-action", "future-epoch", "short-signature", "other-field"} {
		t.Run(name, func(t *testing.T) {
			prior, after := before, result.Action
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(after.Payload, &fields); err != nil {
				t.Fatal(err)
			}
			switch name {
			case "domain":
				after.Domain = "agent_run"
			case "id":
				after.ID = "another_action"
			case "epoch":
				after.ExecutionEpoch++
			case "revision":
				after.Revision++
			case "state":
				after.State = "EFFECT_UNKNOWN"
			case "before-json":
				prior.Payload = []byte("not-json")
			case "after-json":
				after.Payload = []byte("not-json")
			case "existing-marker":
				prior = after
				prior.Revision--
				prior.State = "EXECUTING"
			case "missing-marker":
				delete(fields, agentPlanSettlementField)
				after.Payload, _ = json.Marshal(fields)
			case "invalid-marker-json":
				fields[agentPlanSettlementField] = json.RawMessage(`{}`)
				after.Payload, _ = json.Marshal(fields)
			case "invalid-protobuf":
				fields[agentPlanSettlementField], _ = json.Marshal([]byte{255})
				after.Payload, _ = json.Marshal(fields)
			case "other-field":
				fields["unrelated"] = json.RawMessage(`true`)
				after.Payload, _ = json.Marshal(fields)
			default:
				after = alterPlanSettlementReceipt(t, after, func(r *agentv1.AgentPlanProjectionReceipt) {
					switch name {
					case "schema":
						r.SchemaVersion = 2
					case "missing-evidence":
						r.Evidence = nil
					case "missing-fence":
						r.Evidence.SettlementFence = nil
					case "foreign-action":
						r.Evidence.SettlementFence.ActionId = "another_action"
					case "future-epoch":
						r.Evidence.SettlementFence.ExecutionEpoch++
					case "short-signature":
						r.Signature = r.Signature[:63]
					}
				})
			}
			if nativeAgentPlanSettlementEdge(prior, after) {
				t.Fatal("malformed history widened the private settlement edge")
			}
		})
	}
}

func TestRetainedPlanSettlementProofIsVerifiedBelowOpaqueRecordBytes(t *testing.T) {
	for _, name := range []string{"invalid-payload", "nonterminal", "invalid-marker", "invalid-protobuf", "missing-evidence", "noncanonical", "wrong-phase", "invalid-node-map", "invalid-source-map", "missing-leader", "missing-metadata", "invalid-writer", "bad-signature"} {
		t.Run(name, func(t *testing.T) {
			control, input, _ := planSettlementFixture(t, false)
			result, err := control.SettleAgentPlan(input)
			if err != nil {
				t.Fatal(err)
			}
			record := result.Action
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(record.Payload, &fields); err != nil {
				t.Fatal(err)
			}
			var view, statement string
			switch name {
			case "invalid-payload":
				record.Payload = []byte("not-json")
			case "nonterminal":
				record.State = "EXECUTING"
			case "invalid-marker":
				fields[agentPlanSettlementField] = json.RawMessage(`{}`)
			case "invalid-protobuf":
				fields[agentPlanSettlementField], _ = json.Marshal([]byte{255})
			case "noncanonical":
				var encoded []byte
				if err := json.Unmarshal(fields[agentPlanSettlementField], &encoded); err != nil {
					t.Fatal(err)
				}
				// Repeated singular schema_version decodes but cannot be retained canonically.
				fields[agentPlanSettlementField], _ = json.Marshal(append(encoded, 8, 1))
			case "wrong-phase":
				var binding map[string]json.RawMessage
				if err := json.Unmarshal(fields["nativeAgentExecution"], &binding); err != nil {
					t.Fatal(err)
				}
				binding["phase"] = json.RawMessage(`"tasks"`)
				fields["nativeAgentExecution"], _ = json.Marshal(binding)
			case "invalid-node-map":
				fields[agentNodeDispatchField] = json.RawMessage(`[]`)
			case "invalid-source-map":
				fields[agentNodeOutcomeField] = json.RawMessage(`[]`)
			case "missing-leader":
				fields[agentNodeOutcomeField] = json.RawMessage(`{}`)
			case "missing-metadata":
				view = "control_events"
				statement = "CREATE TEMP VIEW control_events AS SELECT * FROM main.control_events WHERE domain<>'agent_run'"
			case "invalid-writer":
				view = "control_events"
				statement = "CREATE TEMP VIEW control_events AS SELECT event_id,domain,record_id,revision,execution_epoch,state,payload_json,record_digest,CASE WHEN domain='action' THEN 'not-an-integer' ELSE writer_fencing_token END AS writer_fencing_token,recorded_at FROM main.control_events"
			case "missing-evidence":
				record = alterPlanSettlementReceipt(t, record, func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence = nil })
			case "bad-signature":
				record = alterPlanSettlementReceipt(t, record, func(r *agentv1.AgentPlanProjectionReceipt) { r.Signature[0] ^= 1 })
			}
			if name != "invalid-payload" && name != "missing-evidence" && name != "bad-signature" {
				record.Payload, _ = json.Marshal(fields)
			}
			if statement != "" {
				if _, err := control.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentPlanSettlementTx(tx, record)
			_ = tx.Rollback()
			if err == nil {
				t.Fatal("corrupt retained phase proof was accepted")
			}
			if view != "" {
				if _, err := control.db.Exec("DROP VIEW temp." + view); err != nil {
					t.Fatal(err)
				}
			}
			current, _, err := control.Get("action", input.ActionID)
			if err != nil || !bytes.Equal(current.Payload, result.Action.Payload) {
				t.Fatal("read verification changed durable proof")
			}
			if _, _, _, _, err := control.AgentNodeOutcomeSnapshot(input.ActionID, "leader"); err != nil {
				t.Fatalf("original verified proof was damaged: %v", err)
			}
		})
	}
}

func TestAgentPlanSettlementDoesNotGrantGenericTerminalWrites(t *testing.T) {
	control, input, _ := planSettlementFixture(t, false)
	before, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(before.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(input.Projection)
	if err != nil {
		t.Fatal(err)
	}
	fields[agentPlanSettlementField], _ = json.Marshal(encoded)
	for _, terminal := range []string{"SUCCEEDED", "EXECUTING"} {
		forged := before
		forged.Revision++
		forged.State = terminal
		forged.Payload, _ = json.Marshal(fields)
		if err := control.Put(forged); !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("generic writer installed a signed marker: %v", err)
		}
	}
}
