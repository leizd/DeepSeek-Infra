package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
	"google.golang.org/protobuf/proto"
)

func taskBoundaryActionView(t *testing.T, control *Control, record Record) func() {
	t.Helper()
	var err error
	record.Payload, err = canonicalControlPayload(record.Payload)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := protocol.Digest(record)
	if err != nil {
		t.Fatal(err)
	}
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }
	query := fmt.Sprintf(`CREATE TEMP VIEW action_journal AS SELECT id,revision,execution_epoch,state,
	 CASE WHEN id=%s THEN %s ELSE payload_json END AS payload_json,
	 CASE WHEN id=%s THEN %s ELSE record_digest END AS record_digest,
	 writer_fencing_token,updated_at FROM main.action_journal`, quote(record.ID), quote(string(record.Payload)), quote(record.ID), quote(digest))
	if _, err := control.db.Exec(query); err != nil {
		t.Fatal(err)
	}
	return func() {
		if _, err := control.db.Exec("DROP VIEW temp.action_journal"); err != nil {
			t.Fatal(err)
		}
	}
}

func taskMutationBoundaryFixture(t *testing.T, phase string) (*Control, string, func() error) {
	t.Helper()
	switch phase {
	case "claim":
		control := agentExecutionControl(t, true)
		input := agentExecutionRequest(t, control)
		return control, input.OperationID, func() error { _, err := control.ClaimAgentRunExecution(input); return err }
	case "outcome":
		control, input, _ := agentOutcomeFixture(t)
		return control, input.OperationID, func() error { _, err := control.RecordAgentNodeOutcome(input); return err }
	case "plan":
		control, input, _ := planSettlementFixture(t, false)
		return control, input.OperationID, func() error { _, err := control.SettleAgentPlan(input); return err }
	case "settlement":
		control, input, _ := taskSettlementFixture(t, false)
		return control, input.OperationID, func() error { _, err := control.SettleAgentTask(input); return err }
	case "completion":
		control, input := tasksCompletionFixture(t, false)
		return control, input.OperationID, func() error { _, err := control.CompleteAgentTasks(input); return err }
	default:
		t.Fatal("invalid fixture phase")
		return nil, "", nil
	}
}

func TestAgentMutationIgnoredReceiptCannotCommitOrLoseReplay(t *testing.T) {
	for _, phase := range []string{"claim", "outcome", "plan", "settlement", "completion"} {
		t.Run(phase, func(t *testing.T) {
			control, operation, apply := taskMutationBoundaryFixture(t, phase)
			before, err := control.ExportSnapshot()
			if err != nil {
				t.Fatal(err)
			}
			events := countControlEvents(t, control)
			if _, err := control.db.Exec("CREATE TEMP TRIGGER task_receipt_ignore BEFORE INSERT ON control_operator_mutations BEGIN SELECT RAISE(IGNORE); END"); err != nil {
				t.Fatal(err)
			}
			err = apply()
			if _, dropErr := control.db.Exec("DROP TRIGGER task_receipt_ignore"); dropErr != nil {
				t.Fatal(dropErr)
			}
			if !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("%s committed without its replay receipt: %v", phase, err)
			}
			after, err := control.ExportSnapshot()
			if err != nil || after.Digest != before.Digest || countControlEvents(t, control) != events {
				t.Fatalf("ignored receipt retained phase state: %v", err)
			}
			if _, found, err := control.ReadOperatorMutation(operation); err != nil || found {
				t.Fatal("ignored insert retained a receipt")
			}
			if err := apply(); err != nil {
				t.Fatalf("receipt rollback poisoned recovery: %v", err)
			}
			if _, found, err := control.ReadOperatorMutation(operation); err != nil || !found {
				t.Fatal("successful recovery lacks immutable receipt")
			}
			committed := countControlEvents(t, control)
			if err := apply(); err != nil || countControlEvents(t, control) != committed {
				t.Fatalf("recovery replay duplicated phase state: %v", err)
			}
		})
	}
}

func TestTaskTransactionCommitFailurePreservesFreshAndReplayedEffects(t *testing.T) {
	for _, phase := range []string{"settlement", "completion"} {
		for _, replay := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/replay=%t", phase, replay), func(t *testing.T) {
				control, operation, apply := taskMutationBoundaryFixture(t, phase)
				if replay {
					if err := apply(); err != nil {
						t.Fatal(err)
					}
				}
				before, err := control.ExportSnapshot()
				if err != nil {
					t.Fatal(err)
				}
				events := countControlEvents(t, control)
				for _, statement := range []string{
					"CREATE TEMP TABLE task_commit_parent(id INTEGER PRIMARY KEY)",
					"CREATE TEMP TABLE task_commit_child(id INTEGER REFERENCES task_commit_parent(id) DEFERRABLE INITIALLY DEFERRED)",
					"CREATE TEMP TRIGGER task_commit_failure AFTER UPDATE ON control_writer BEGIN INSERT INTO task_commit_child VALUES(1); END",
				} {
					if _, err := control.db.Exec(statement); err != nil {
						t.Fatal(err)
					}
				}
				if err := apply(); err == nil {
					t.Fatal("deferred commit failure returned successful task state")
				}
				if _, err := control.db.Exec("DROP TRIGGER task_commit_failure"); err != nil {
					t.Fatal(err)
				}
				after, err := control.ExportSnapshot()
				if err != nil || after.Digest != before.Digest || countControlEvents(t, control) != events {
					t.Fatalf("failed commit changed task state: %v", err)
				}
				if _, found, err := control.ReadOperatorMutation(operation); err != nil || found != replay {
					t.Fatal("commit failure changed original replay history")
				}
				if err := apply(); err != nil {
					t.Fatalf("failed commit damaged recovery: %v", err)
				}
			})
		}
	}
}

func TestTaskTransactionsRevalidateMutableRowsAfterSuccessfulSQLWrites(t *testing.T) {
	for _, phase := range []string{"settlement", "completion"} {
		for _, table := range []string{"action_journal", "agent_runs"} {
			if phase == "completion" && table == "agent_runs" {
				continue
			}
			t.Run(phase+"/"+table, func(t *testing.T) {
				control, operation, apply := taskMutationBoundaryFixture(t, phase)
				before, err := control.ExportSnapshot()
				if err != nil {
					t.Fatal(err)
				}
				events := countControlEvents(t, control)
				fault := fmt.Sprintf("CREATE TEMP TRIGGER task_readback_failure AFTER UPDATE ON %s BEGIN UPDATE %s SET payload_json='{}' WHERE id=NEW.id; END", table, table)
				if _, err := control.db.Exec(fault); err != nil {
					t.Fatal(err)
				}
				if err := apply(); err == nil {
					t.Fatal("successful SQL write hid an inconsistent mutable projection")
				}
				if _, err := control.db.Exec("DROP TRIGGER task_readback_failure"); err != nil {
					t.Fatal(err)
				}
				after, err := control.ExportSnapshot()
				if err != nil || after.Digest != before.Digest || countControlEvents(t, control) != events {
					t.Fatalf("corrupt projection escaped transaction rollback: %v", err)
				}
				if _, found, err := control.ReadOperatorMutation(operation); err != nil || found {
					t.Fatal("corrupt projection acquired replay receipt")
				}
				if err := apply(); err != nil {
					t.Fatalf("readback refusal poisoned recovery: %v", err)
				}
			})
		}
	}
}

func TestTaskAPIsRejectReboundOriginalProofThroughActualEntryPoints(t *testing.T) {
	for _, phase := range []string{"settlement", "completion", "completion-replay"} {
		t.Run(phase, func(t *testing.T) {
			var control *Control
			var action Record
			var runID string
			var apply func() error
			if phase == "settlement" {
				var input AgentTaskSettlementRequest
				control, input, _ = taskSettlementFixture(t, false)
				result, err := control.SettleAgentTask(input)
				if err != nil {
					t.Fatal(err)
				}
				action, runID = result.Action, input.RunID
				apply = func() error { _, err := control.SettleAgentTask(input); return err }
			} else {
				var input CompleteAgentTasksRequest
				control, input = tasksCompletionFixture(t, false)
				if phase == "completion-replay" {
					if _, err := control.CompleteAgentTasks(input); err != nil {
						t.Fatal(err)
					}
				}
				var err error
				action, _, err = control.Get("action", input.ActionID)
				if err != nil {
					t.Fatal(err)
				}
				runID = input.RunID
				apply = func() error { _, err := control.CompleteAgentTasks(input); return err }
			}
			binding, err := decodeAgentExecution(action)
			if err != nil {
				t.Fatal(err)
			}
			before, err := control.ExportSnapshot()
			if err != nil {
				t.Fatal(err)
			}
			restore := taskBoundaryHistoryView(t, control, "agent_run", runID, binding.MetadataIndex, func(record *Record) {
				var run agent.RunMetadata
				if err := json.Unmarshal(record.Payload, &run); err != nil {
					t.Fatal(err)
				}
				run.Request.SHA256 = strings.Repeat("9", 64)
				record.Payload, _ = json.Marshal(run)
			})
			err = apply()
			restore()
			if !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("actual %s API accepted rebound immutable source: %v", phase, err)
			}
			after, err := control.ExportSnapshot()
			if err != nil || after.Digest != before.Digest {
				t.Fatalf("rebound source changed live projections: %v", err)
			}
			if err := apply(); err != nil {
				t.Fatalf("source proof refusal damaged valid retry: %v", err)
			}
		})
	}
}

func TestTaskSettlementHistoryRejectsMarkerFromAnotherRevision(t *testing.T) {
	control, input, _ := taskSettlementFixture(t, false)
	result, err := control.SettleAgentTask(input)
	if err != nil {
		t.Fatal(err)
	}
	markers, err := readAgentTaskSettlements(result.Action)
	if err != nil {
		t.Fatal(err)
	}
	marker := markers[input.NodeID]
	marker.ActionRevision--
	markers[input.NodeID] = marker
	forged := taskBoundaryField(t, result.Action, agentTaskSettlementField, markers)
	restoreAction := taskBoundaryActionView(t, control, forged)
	restoreHistory := taskBoundaryHistoryView(t, control, "action", input.ActionID, result.Action.Revision, func(record *Record) { *record = forged })
	if _, _, err := control.Get("action", input.ActionID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("consistent checksums hid another-revision settlement: %v", err)
	}
	restoreHistory()
	restoreAction()
	if replay, err := control.SettleAgentTask(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("rebound marker damaged valid settlement: %v", err)
	}
}

func TestTaskSnapshotReadFailuresReturnNoPartialExport(t *testing.T) {
	for _, kind := range []string{"query", "late-row"} {
		t.Run(kind, func(t *testing.T) {
			control, _, _ := taskSettlementFixture(t, false)
			before, err := control.ExportSnapshot()
			if err != nil {
				t.Fatal(err)
			}
			query := "CREATE TEMP VIEW agent_runs AS SELECT id FROM main.agent_runs"
			if kind == "late-row" {
				query = `CREATE TEMP VIEW agent_runs AS SELECT id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,updated_at FROM main.agent_runs
				 UNION ALL SELECT json_extract('invalid-json','$'),revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,updated_at FROM main.agent_runs`
			}
			if _, err := control.db.Exec(query); err != nil {
				t.Fatal(err)
			}
			partial, err := control.ExportSnapshot()
			if err == nil || partial.Digest != "" || len(partial.Records) != 0 {
				t.Fatalf("failed snapshot exposed a partial export: %v", err)
			}
			if _, err := control.db.Exec("DROP VIEW temp.agent_runs"); err != nil {
				t.Fatal(err)
			}
			after, err := control.ExportSnapshot()
			if err != nil || after.Digest != before.Digest || !bytes.Equal(before.Records[0].Payload, after.Records[0].Payload) {
				t.Fatalf("export read failure damaged original store: %v", err)
			}
		})
	}
}

func TestAgentPhaseReplayRefusesAnotherJournalDomain(t *testing.T) {
	for _, phase := range []string{"claim", "outcome", "plan"} {
		t.Run(phase, func(t *testing.T) {
			control, _, apply := taskMutationBoundaryFixture(t, phase)
			if err := apply(); err != nil {
				t.Fatal(err)
			}
			before, err := control.ExportSnapshot()
			if err != nil {
				t.Fatal(err)
			}
			query := `CREATE TEMP VIEW control_operator_mutations AS SELECT operation_id,'policy' AS domain,record_id,record_revision,record_state,payload_digest,request_digest,action_id,execution_epoch,cutover_revision,fencing_token,writer_fencing_token,actor,recorded_at FROM main.control_operator_mutations`
			if _, err := control.db.Exec(query); err != nil {
				t.Fatal(err)
			}
			err = apply()
			if _, dropErr := control.db.Exec("DROP VIEW temp.control_operator_mutations"); dropErr != nil {
				t.Fatal(dropErr)
			}
			if err == nil {
				t.Fatal("another domain's journal receipt replayed an Agent effect")
			}
			after, err := control.ExportSnapshot()
			if err != nil || before.Digest != after.Digest {
				t.Fatalf("refused replay changed control state: %v", err)
			}
			if err := apply(); err != nil {
				t.Fatalf("replay binding refusal damaged original receipt: %v", err)
			}
		})
	}
}

func TestLegacySignedReceiptCannotBeReinterpretedAsCompletedTask(t *testing.T) {
	control, input, private := taskSettlementFixture(t, false)
	result, err := control.SettleAgentTask(input)
	if err != nil {
		t.Fatal(err)
	}
	values, err := agentNodeOutcomeBytes(result.Action)
	if err != nil {
		t.Fatal(err)
	}
	source := values[input.NodeID]
	var receipt agentv1.AgentNodeOutcomeReceipt
	if err := proto.Unmarshal(source.Receipt, &receipt); err != nil {
		t.Fatal(err)
	}
	receipt.Evidence.TaskOutput = nil
	agentOutcomeSign(t, &receipt, private)
	source.Receipt, err = proto.MarshalOptions{Deterministic: true}.Marshal(&receipt)
	if err != nil {
		t.Fatal(err)
	}
	values[input.NodeID] = source
	markers, err := readAgentTaskSettlements(result.Action)
	if err != nil {
		t.Fatal(err)
	}
	marker := markers[input.NodeID]
	digest := sha256.Sum256(source.Receipt)
	marker.SourceReceiptSHA256 = hex.EncodeToString(digest[:])
	markers[input.NodeID] = marker
	forged := taskBoundaryField(t, result.Action, agentNodeOutcomeField, values)
	forged = taskBoundaryField(t, forged, agentTaskSettlementField, markers)
	// The legacy receipt is genuinely signed and readable. It still supplies no
	// typed task counters, so a matching source hash cannot certify completion.
	nodes, err := readAgentNodeDispatches(forged)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.agentNodeOutcomes(forged, nodes); err != nil {
		t.Fatalf("legacy signature fixture is invalid: %v", err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	_, err = control.validatedAgentTaskSettlementsTx(tx, forged)
	_ = tx.Rollback()
	if !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("legacy proof invented task completion: %v", err)
	}
	if replay, err := control.SettleAgentTask(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("legacy rejection damaged original proof: %v", err)
	}
}
