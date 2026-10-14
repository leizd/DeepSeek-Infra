package store

import (
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func synthesisChangePayload(t *testing.T, record *Record, change func(map[string]json.RawMessage)) {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(record.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	change(fields)
	record.Payload, _ = json.Marshal(fields)
}

func synthesisReplaceFinalProof(t *testing.T, record *Record, change func(*agentv1.AgentSynthesisProjectionReceipt)) {
	t.Helper()
	synthesisChangePayload(t, record, func(fields map[string]json.RawMessage) {
		var encoded []byte
		var receipt agentv1.AgentSynthesisProjectionReceipt
		if json.Unmarshal(fields[agentSynthesisSettlementField], &encoded) != nil || proto.Unmarshal(encoded, &receipt) != nil {
			t.Fatal("fixture final proof is invalid")
		}
		change(&receipt)
		encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
		if err != nil {
			t.Fatal(err)
		}
		fields[agentSynthesisSettlementField], _ = json.Marshal(encoded)
	})
}

func TestSynthesisHistoricalValidationRejectsMalformedAndReboundRetainedProofs(t *testing.T) {
	control, input, private := synthesisSettlementFixture(t)
	result, err := control.SettleAgentSynthesis(input)
	if err != nil {
		t.Fatal(err)
	}
	before := synthesisSnapshot(t, control)
	cases := map[string]func(*Record){
		"malformed-json": func(r *Record) { r.Payload = json.RawMessage("{") },
		"not-terminal":   func(r *Record) { r.State = "EXECUTING" },
		"bad-base64": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) {
				f[agentSynthesisSettlementField] = json.RawMessage(`"invalid-base64"`)
			})
		},
		"bad-protobuf": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f[agentSynthesisSettlementField], _ = json.Marshal([]byte{0xff}) })
		},
		"no-evidence": func(r *Record) {
			synthesisReplaceFinalProof(t, r, func(p *agentv1.AgentSynthesisProjectionReceipt) { p.Evidence = nil })
		},
		"noncanonical": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) {
				var encoded []byte
				_ = json.Unmarshal(f[agentSynthesisSettlementField], &encoded)
				f[agentSynthesisSettlementField], _ = json.Marshal(append(encoded, 0x08, 0x01))
			})
		},
		"invalid-execution": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f["nativeAgentExecution"] = json.RawMessage(`{}`) })
		},
		"invalid-dispatch": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f[agentNodeDispatchField] = json.RawMessage(`[]`) })
		},
		"invalid-outcomes": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f[agentNodeOutcomeField] = json.RawMessage(`[]`) })
		},
		"no-source": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f[agentNodeOutcomeField] = json.RawMessage(`{}`) })
		},
		"bad-final-signature": func(r *Record) {
			synthesisReplaceFinalProof(t, r, func(p *agentv1.AgentSynthesisProjectionReceipt) { p.Signature[0] ^= 1 })
		},
		"source-failed": func(r *Record) {
			outcomes, err := agentNodeOutcomeBytes(*r)
			if err != nil {
				t.Fatal(err)
			}
			source := outcomes["synthesizer"]
			var receipt agentv1.AgentNodeOutcomeReceipt
			if err := proto.Unmarshal(source.Receipt, &receipt); err != nil {
				t.Fatal(err)
			}
			receipt.Evidence.TaskOutput.Failed = true
			agentOutcomeSign(t, &receipt, private)
			source.Receipt, err = proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
			if err != nil {
				t.Fatal(err)
			}
			outcomes["synthesizer"] = source
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f[agentNodeOutcomeField], _ = json.Marshal(outcomes) })
		},
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			candidate := result.Action
			candidate.Payload = append(json.RawMessage(nil), candidate.Payload...)
			change(&candidate)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentSynthesisSettlementTx(tx, candidate)
			_ = tx.Rollback()
			if err == nil {
				t.Fatal("malformed or rebound retained proof was accepted")
			}
		})
	}
	if synthesisSnapshot(t, control) != before {
		t.Fatal("historical proof rejection changed durable state")
	}
}

func synthesisTemporaryView(t *testing.T, control *Control, table, query string) func() {
	t.Helper()
	if _, err := control.db.Exec("CREATE TEMP VIEW " + table + " AS " + query); err != nil {
		t.Fatal(err)
	}
	return func() {
		if _, err := control.db.Exec("DROP VIEW temp." + table); err != nil {
			t.Fatal(err)
		}
	}
}

func TestSynthesisHistoryRequiresItsOwnTerminationAndReleasedResources(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	result, err := control.SettleAgentSynthesis(input)
	if err != nil {
		t.Fatal(err)
	}
	quotedID := "'" + strings.ReplaceAll(input.ActionID, "'", "''") + "'"
	cases := []struct{ name, table, query string }{
		{"missing-termination", "action_lease_events", "SELECT * FROM main.action_lease_events WHERE NOT(action_id=" + quotedID + " AND event_type='TERMINATED')"},
		{"missing-terminal-lease", "action_leases", "SELECT * FROM main.action_leases WHERE action_id!=" + quotedID},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			restore := synthesisTemporaryView(t, control, test.table, test.query)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentSynthesisSettlementTx(tx, result.Action)
			_ = tx.Rollback()
			restore()
			if err == nil {
				t.Fatal("missing synthesis termination evidence was accepted")
			}
		})
	}
	// An unreleased resource must refuse historical completion, even when all
	// signed bodies and termination evidence remain valid.
	columns, err := control.db.Query("PRAGMA main.table_info(action_resource_leases)")
	if err != nil {
		t.Fatal(err)
	}
	var selections []string
	for columns.Next() {
		var number, notNull, primary int
		var name, kind string
		var defaultValue any
		if err := columns.Scan(&number, &name, &kind, &notNull, &defaultValue, &primary); err != nil {
			t.Fatal(err)
		}
		value := "0"
		if name == "action_id" {
			value = quotedID
		} else if strings.Contains(kind, "TEXT") {
			value = "'fixture-resource'"
		}
		selections = append(selections, value+" AS "+name)
	}
	if err := columns.Close(); err != nil {
		t.Fatal(err)
	}
	restore := synthesisTemporaryView(t, control, "action_resource_leases", "SELECT * FROM main.action_resource_leases UNION ALL SELECT "+strings.Join(selections, ","))
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	err = control.validateAgentSynthesisSettlementTx(tx, result.Action)
	_ = tx.Rollback()
	restore()
	if err == nil {
		t.Fatal("unreleased synthesis resource authorized completion")
	}
	// Restoring the real tables must restore the original proof, without repair writes.
	tx, err = control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	err = control.validateAgentSynthesisSettlementTx(tx, result.Action)
	_ = tx.Rollback()
	if err != nil {
		t.Fatal("original termination proof did not recover", err)
	}
}

func TestSynthesisSettlementPrivateEdgePreservesEveryPriorFieldAndFence(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	before, found, err := control.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	result, err := control.SettleAgentSynthesis(input)
	if err != nil {
		t.Fatal(err)
	}
	if !nativeAgentSynthesisSettlementEdge(before, result.Action) {
		t.Fatal("valid final settlement edge was rejected")
	}
	cases := map[string]func(*Record){
		"domain":   func(r *Record) { r.Domain = "agent_run" },
		"id":       func(r *Record) { r.ID = "different" },
		"epoch":    func(r *Record) { r.ExecutionEpoch++ },
		"revision": func(r *Record) { r.Revision++ },
		"state":    func(r *Record) { r.State = "EXECUTING" },
		"payload":  func(r *Record) { r.Payload = json.RawMessage("{") },
		"extra-field": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f["unexpected"] = json.RawMessage(`true`) })
		},
		"changed-prior-field": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f["nativeAgentExecution"] = json.RawMessage(`{}`) })
		},
		"missing-marker": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { delete(f, agentSynthesisSettlementField) })
		},
		"bad-marker": func(r *Record) {
			synthesisChangePayload(t, r, func(f map[string]json.RawMessage) { f[agentSynthesisSettlementField] = json.RawMessage(`true`) })
		},
		"schema": func(r *Record) {
			synthesisReplaceFinalProof(t, r, func(p *agentv1.AgentSynthesisProjectionReceipt) { p.SchemaVersion = 2 })
		},
		"no-fence": func(r *Record) {
			synthesisReplaceFinalProof(t, r, func(p *agentv1.AgentSynthesisProjectionReceipt) { p.Evidence.SettlementFence = nil })
		},
		"bad-signature-size": func(r *Record) {
			synthesisReplaceFinalProof(t, r, func(p *agentv1.AgentSynthesisProjectionReceipt) { p.Signature = make([]byte, ed25519.SignatureSize-1) })
		},
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			after := result.Action
			after.Payload = append(json.RawMessage(nil), after.Payload...)
			change(&after)
			if nativeAgentSynthesisSettlementEdge(before, after) {
				t.Fatal("foreign or rebound private settlement edge was accepted")
			}
		})
	}
	if nativeAgentSynthesisSettlementEdge(result.Action, result.Action) {
		t.Fatal("existing private marker could be rewritten")
	}
}

func TestSynthesisSettlementRefusesInvalidEnvelopeAndChangedTerminalOperation(t *testing.T) {
	cases := map[string]func(*AgentSynthesisSettlementRequest){
		"invalid-protobuf-text": func(i *AgentSynthesisSettlementRequest) { i.Projection.SignerPublicKey = string([]byte{0xff}) },
		"oversize-proof": func(i *AgentSynthesisSettlementRequest) {
			i.Projection.Evidence.RunId = strings.Repeat("x", maximumPayloadBytes+1)
		},
		"operation":      func(i *AgentSynthesisSettlementRequest) { i.OperationID = "" },
		"actor":          func(i *AgentSynthesisSettlementRequest) { i.Actor = "" },
		"missing-action": func(i *AgentSynthesisSettlementRequest) { i.ActionID = "missing-action" },
		"different-run":  func(i *AgentSynthesisSettlementRequest) { i.RunID = "other-run" },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			control, input, _ := synthesisSettlementFixture(t)
			before := synthesisSnapshot(t, control)
			change(&input)
			if _, err := control.SettleAgentSynthesis(input); err == nil {
				t.Fatal("invalid envelope or operation succeeded")
			}
			if synthesisSnapshot(t, control) != before {
				t.Fatal("invalid envelope changed durable state")
			}
		})
	}
	control, input, _ := synthesisSettlementFixture(t)
	if _, err := control.SettleAgentSynthesis(input); err != nil {
		t.Fatal(err)
	}
	before := synthesisSnapshot(t, control)
	input.OperationID = "second-final-operation"
	if _, err := control.SettleAgentSynthesis(input); err == nil || synthesisSnapshot(t, control) != before {
		t.Fatalf("second final operation succeeded: %v", err)
	}
}

func TestSynthesisSettlementRequiresRecordedSuccessfulSourceProof(t *testing.T) {
	for _, test := range []struct {
		name             string
		recorded, failed bool
	}{{"unrecorded", false, false}, {"failed", true, true}} {
		t.Run(test.name, func(t *testing.T) {
			control, input, _ := synthesisSettlementSourceFixture(t, test.recorded, test.failed)
			before := synthesisSnapshot(t, control)
			if _, err := control.SettleAgentSynthesis(input); err == nil || synthesisSnapshot(t, control) != before {
				t.Fatalf("unrecorded or failed source acquired completion: %v", err)
			}
		})
	}
}

func TestSynthesisSettlementRefusesMissingLeaseResourceAndAdmissionHistory(t *testing.T) {
	for _, table := range []string{"action_leases", "action_resource_leases", "action_lease_events"} {
		t.Run(table, func(t *testing.T) {
			control, input, _ := synthesisSettlementFixture(t)
			before := synthesisSnapshot(t, control)
			quoted := "'" + strings.ReplaceAll(input.ActionID, "'", "''") + "'"
			condition := "action_id!=" + quoted
			if table == "action_lease_events" {
				condition = "NOT(action_id=" + quoted + " AND event_type='CLAIMED')"
			}
			restore := synthesisTemporaryView(t, control, table, "SELECT * FROM main."+table+" WHERE "+condition)
			_, err := control.SettleAgentSynthesis(input)
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatalf("lost authority ledger acquired completion: %v", err)
			}
			if _, err := control.SettleAgentSynthesis(input); err != nil {
				t.Fatal("restoring authority failed to restore valid settlement", err)
			}
		})
	}
}

func TestSynthesisSettlementAndHistoricalReadsRequireOriginalTaskCompletion(t *testing.T) {
	for _, settled := range []bool{false, true} {
		t.Run(fmt.Sprint(settled), func(t *testing.T) {
			control, input, _ := synthesisSettlementFixture(t)
			var result AgentSynthesisSettlementResult
			var err error
			if settled {
				result, err = control.SettleAgentSynthesis(input)
				if err != nil {
					t.Fatal(err)
				}
			}
			before := synthesisSnapshot(t, control)
			var taskID string
			if err := control.db.QueryRow(`SELECT id FROM action_journal WHERE json_extract(payload_json,'$.nativeAgentExecution.runId')=? AND json_extract(payload_json,'$.nativeAgentExecution.phase')='tasks'`, input.RunID).Scan(&taskID); err != nil {
				t.Fatal(err)
			}
			quoted := "'" + strings.ReplaceAll(taskID, "'", "''") + "'"
			restore := synthesisTemporaryView(t, control, "action_journal", "SELECT * FROM main.action_journal WHERE id!="+quoted)
			if settled {
				tx, beginErr := control.db.Begin()
				if beginErr != nil {
					t.Fatal(beginErr)
				}
				err = control.validateAgentSynthesisSettlementTx(tx, result.Action)
				_ = tx.Rollback()
			} else {
				_, err = control.SettleAgentSynthesis(input)
			}
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatalf("missing completed predecessor was accepted: %v", err)
			}
		})
	}
}

func TestSynthesisSettlementRefusesUnreadableRunWithoutAnyPartialEffect(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	before := synthesisSnapshot(t, control)
	run, found, err := control.Get("agent_run", input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	run.Payload = json.RawMessage(`{}`)
	restore := settlementRunReadOverride(t, control, run)
	_, err = control.SettleAgentSynthesis(input)
	restore()
	if err == nil || synthesisSnapshot(t, control) != before {
		t.Fatalf("unreadable run was completed: %v", err)
	}
}

func TestSynthesisFinalProofRequiresRetainedActionWitnessAndLiveReplayWriter(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	result, err := control.SettleAgentSynthesis(input)
	if err != nil {
		t.Fatal(err)
	}
	restore := taskBoundaryHistoryView(t, control, "action", input.ActionID, result.Action.Revision, nil)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	err = control.validateAgentSynthesisSettlementTx(tx, result.Action)
	_ = tx.Rollback()
	restore()
	if err == nil {
		t.Fatal("missing final action witness was accepted")
	}
	restore = taskBoundaryHistoryView(t, control, "action", input.ActionID, result.Action.Revision, func(record *Record) {
		synthesisChangePayload(t, record, func(fields map[string]json.RawMessage) { fields["tampered"] = json.RawMessage(`true`) })
	})
	tx, err = control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	err = control.validateAgentSynthesisSettlementTx(tx, result.Action)
	_ = tx.Rollback()
	restore()
	if err == nil {
		t.Fatal("different retained final action payload was accepted")
	}
	before := synthesisSnapshot(t, control)
	original, calls := control.now, 0
	now := original()
	control.now = func() int64 {
		calls++
		if calls >= 2 {
			return now + control.leaseSeconds
		}
		return now
	}
	_, err = control.SettleAgentSynthesis(input)
	control.now = original
	if err == nil || synthesisSnapshot(t, control) != before {
		t.Fatalf("expired replay writer committed a change: %v", err)
	}
}

func TestSynthesisSettlementOriginalAdmissionWriterCannotBeRebound(t *testing.T) {
	control, input, private := synthesisSettlementFixture(t)
	before := synthesisSnapshot(t, control)
	record, found, err := control.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	outcomes, err := agentNodeOutcomeBytes(record)
	if err != nil {
		t.Fatal(err)
	}
	stored := outcomes["synthesizer"]
	var receipt agentv1.AgentNodeOutcomeReceipt
	if err := proto.Unmarshal(stored.Receipt, &receipt); err != nil {
		t.Fatal(err)
	}
	receipt.Evidence.WriterFencingToken++
	agentOutcomeSign(t, &receipt, private)
	stored.WriterFencingToken++
	stored.Receipt, err = proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
	if err != nil {
		t.Fatal(err)
	}
	outcomes["synthesizer"] = stored
	synthesisChangePayload(t, &record, func(fields map[string]json.RawMessage) { fields[agentNodeOutcomeField], _ = json.Marshal(outcomes) })
	restore := taskBoundaryActionView(t, control, record)
	_, err = control.SettleAgentSynthesis(input)
	restore()
	if err == nil || synthesisSnapshot(t, control) != before {
		t.Fatalf("signed rebound writer bypassed original admission: %v", err)
	}
}

func TestSynthesisReplayRefusesLostOrAlteredPrivateFinalMarker(t *testing.T) {
	for _, altered := range []bool{false, true} {
		t.Run(fmt.Sprint(altered), func(t *testing.T) {
			control, input, _ := synthesisSettlementFixture(t)
			result, err := control.SettleAgentSynthesis(input)
			if err != nil {
				t.Fatal(err)
			}
			before := synthesisSnapshot(t, control)
			record := result.Action
			if altered {
				synthesisReplaceFinalProof(t, &record, func(receipt *agentv1.AgentSynthesisProjectionReceipt) { receipt.Signature[0] ^= 1 })
			} else {
				synthesisChangePayload(t, &record, func(fields map[string]json.RawMessage) { delete(fields, agentSynthesisSettlementField) })
			}
			restore := taskBoundaryActionView(t, control, record)
			_, err = control.SettleAgentSynthesis(input)
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatalf("incomplete or altered terminal proof replayed: %v", err)
			}
		})
	}
}

func TestSynthesisSettlementRollsBackWhenWriterIsFencedAfterLeaseTermination(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	before := synthesisSnapshot(t, control)
	quoted := "'" + strings.ReplaceAll(input.ActionID, "'", "''") + "'"
	query := "SELECT singleton,runtime,mode,owner_instance_id,CASE WHEN EXISTS(SELECT 1 FROM main.action_lease_events WHERE action_id=" + quoted + " AND event_type='TERMINATED') THEN fencing_token+1 ELSE fencing_token END AS fencing_token,lease_until FROM main.control_writer"
	restore := synthesisTemporaryView(t, control, "control_writer", query)
	_, err := control.SettleAgentSynthesis(input)
	restore()
	if err == nil || synthesisSnapshot(t, control) != before {
		t.Fatalf("fenced commit writer published partial synthesis: %v", err)
	}
	if _, err := control.SettleAgentSynthesis(input); err != nil {
		t.Fatal("rollback left a partial fenced settlement", err)
	}
}

func TestSynthesisSettlementLeaseAndWriterExpiryRollBackTheWholeTransaction(t *testing.T) {
	for _, stage := range []string{"expired-action", "backward-time", "commit-action", "commit-writer", "finish-writer", "finish-action", "finish-backward"} {
		t.Run(stage, func(t *testing.T) {
			control, input, _ := synthesisSettlementFixture(t, 5)
			before := synthesisSnapshot(t, control)
			original := control.now
			now, calls := original(), 0
			control.now = func() int64 {
				calls++
				switch stage {
				case "expired-action":
					return now + 5
				case "backward-time":
					return now - 1
				case "commit-action":
					if calls >= 2 {
						return now + 5
					}
				case "commit-writer":
					if calls >= 2 {
						return now + control.leaseSeconds
					}
				case "finish-action":
					if calls >= 3 {
						return now + 5
					}
				case "finish-backward":
					if calls >= 3 {
						return now - 1
					}
				case "finish-writer":
					if calls >= 3 {
						return now + control.leaseSeconds
					}
				}
				return now
			}
			_, err := control.SettleAgentSynthesis(input)
			control.now = original
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatalf("expired authority committed a partial final state: %v", err)
			}
			if _, err := control.SettleAgentSynthesis(input); err != nil {
				t.Fatal("refused transaction left an unreplayable partial state", err)
			}
		})
	}
}

func TestSynthesisSettlementLeaseUpdateAndResourceDeleteFailuresRemainAtomic(t *testing.T) {
	for _, test := range []struct{ table, event string }{{"action_leases", "UPDATE"}, {"action_resource_leases", "DELETE"}} {
		t.Run(test.table, func(t *testing.T) {
			control, input, _ := synthesisSettlementFixture(t)
			before := synthesisSnapshot(t, control)
			if _, err := control.db.Exec(fmt.Sprintf("CREATE TEMP TRIGGER refuse_synthesis BEFORE %s ON %s BEGIN SELECT RAISE(ABORT,'isolated synthesis storage failure'); END", test.event, test.table)); err != nil {
				t.Fatal(err)
			}
			_, err := control.SettleAgentSynthesis(input)
			if _, dropErr := control.db.Exec("DROP TRIGGER temp.refuse_synthesis"); dropErr != nil {
				t.Fatal(dropErr)
			}
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatalf("storage failure committed final state: %v", err)
			}
			if _, err := control.SettleAgentSynthesis(input); err != nil {
				t.Fatal("storage failure left a partial operator receipt", err)
			}
		})
	}
}

// maximumPayloadBytes bounds serialized control/proof payloads. Actor is an
// authenticated string label with no separate schema length limit. Preserve that
// contract and require the complete label to remain bound to the replay digest.
func TestSynthesisActorLabelIsBoundToExactOperatorReplay(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	input.Actor = strings.Repeat("x", maximumPayloadBytes+1)
	result, err := control.SettleAgentSynthesis(input)
	if err != nil || result.Run.Status != "done" {
		t.Fatal("valid actor string was rejected", err)
	}
	replay, err := control.SettleAgentSynthesis(input)
	if err != nil || !replay.AlreadyApplied {
		t.Fatal("exact actor label did not replay", err)
	}
	before := synthesisSnapshot(t, control)
	input.Actor += "changed"
	if _, err := control.SettleAgentSynthesis(input); err == nil || synthesisSnapshot(t, control) != before {
		t.Fatalf("changed actor label reused a terminal operation: %v", err)
	}
}
