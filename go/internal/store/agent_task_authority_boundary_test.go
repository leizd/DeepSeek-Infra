package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func taskBoundaryField(t *testing.T, record Record, key string, value any) Record {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(record.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	var err error
	if value == nil {
		delete(fields, key)
	} else if raw, ok := value.(json.RawMessage); ok {
		fields[key] = raw
	} else {
		fields[key], err = json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
	}
	record.Payload, err = json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return record
}

// Corrupt only reads from this disposable connection. Real immutable rows,
// schema triggers and their bytes remain intact for the recovery assertion.
func taskBoundaryHistoryView(t *testing.T, control *Control, domain, id string, revision int64, change func(*Record)) func() {
	t.Helper()
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }
	condition := fmt.Sprintf("domain=%s AND record_id=%s AND revision=%d", quote(domain), quote(id), revision)
	query := "CREATE TEMP VIEW control_events AS SELECT * FROM main.control_events WHERE NOT (" + condition + ")"
	if change != nil {
		record, _, found, err := scanControlRecord(control.db.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM main.control_events WHERE domain=? AND record_id=? AND revision=?`, domain, id, revision), domain)
		if err != nil || !found {
			t.Fatalf("missing original immutable checkpoint: %v", err)
		}
		change(&record)
		record.Payload, err = canonicalControlPayload(record.Payload)
		if err != nil {
			t.Fatal(err)
		}
		digest, err := protocol.Digest(record)
		if err != nil {
			t.Fatal(err)
		}
		query = fmt.Sprintf(`CREATE TEMP VIEW control_events AS SELECT event_id,domain,record_id,revision,
		 CASE WHEN %s THEN %d ELSE execution_epoch END AS execution_epoch,
		 CASE WHEN %s THEN %s ELSE state END AS state,
		 CASE WHEN %s THEN %s ELSE payload_json END AS payload_json,
		 CASE WHEN %s THEN %s ELSE record_digest END AS record_digest,
		 writer_fencing_token,recorded_at FROM main.control_events`, condition, record.ExecutionEpoch, condition, quote(record.State), condition, quote(string(record.Payload)), condition, quote(digest))
	}
	if _, err := control.db.Exec(query); err != nil {
		t.Fatal(err)
	}
	return func() {
		if _, err := control.db.Exec("DROP VIEW temp.control_events"); err != nil {
			t.Fatal(err)
		}
	}
}

func TestTasksCompletionPrivateEdgePreservesEveryOriginalField(t *testing.T) {
	control, input := tasksCompletionFixture(t, false)
	before, found, err := control.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	result, err := control.CompleteAgentTasks(input)
	if err != nil || !nativeAgentTasksCompletionEdge(before, result.Action) {
		t.Fatalf("valid fenced phase completion was refused: %v", err)
	}
	for name, change := range map[string]func(Record, Record) (Record, Record){
		"changed-unrelated-field": func(a, b Record) (Record, Record) { return a, taskBoundaryField(t, b, "unrelated", true) },
		"removed-task-markers":    func(a, b Record) (Record, Record) { return a, taskBoundaryField(t, b, agentTaskSettlementField, nil) },
		"missing-completion":      func(a, b Record) (Record, Record) { return a, taskBoundaryField(t, b, agentTasksCompletionField, nil) },
		"existing-completion": func(a, b Record) (Record, Record) {
			return taskBoundaryField(t, a, agentTasksCompletionField, json.RawMessage(`{}`)), b
		},
		"invalid-completion": func(a, b Record) (Record, Record) {
			return a, taskBoundaryField(t, b, agentTasksCompletionField, json.RawMessage(`[]`))
		},
		"wrong-domain":   func(a, b Record) (Record, Record) { b.Domain = "agent_run"; return a, b },
		"wrong-action":   func(a, b Record) (Record, Record) { b.ID = "another-action"; return a, b },
		"wrong-state":    func(a, b Record) (Record, Record) { b.State = "FAILED"; return a, b },
		"wrong-revision": func(a, b Record) (Record, Record) { b.Revision++; return a, b },
		"wrong-epoch":    func(a, b Record) (Record, Record) { b.ExecutionEpoch++; return a, b },
	} {
		t.Run(name, func(t *testing.T) {
			a, b := change(before, result.Action)
			if nativeAgentTasksCompletionEdge(a, b) {
				t.Fatal("completion edge altered existing authority or private evidence")
			}
		})
	}
	// EXECUTING -> SUCCEEDED is normally legal. Its generic legality must not
	// bypass the stricter completion edge when immutable history is reopened.
	restore := taskBoundaryHistoryView(t, control, "action", input.ActionID, result.Action.Revision, func(record *Record) {
		*record = taskBoundaryField(t, *record, "unrelated", true)
	})
	if _, _, err := control.Get("action", input.ActionID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("generic terminal transition bypassed completion preservation: %v", err)
	}
	restore()
	if replay, err := control.CompleteAgentTasks(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("refusing forged history damaged the original phase: %v", err)
	}
}

func TestTasksCompletionWriterRejectsUnrelatedPayloadChangesBeforeWriting(t *testing.T) {
	control, input := tasksCompletionFixture(t, false)
	before, found, err := control.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	markers, err := readAgentTaskSettlements(before)
	if err != nil {
		t.Fatal(err)
	}
	binding, err := decodeAgentExecution(before)
	if err != nil {
		t.Fatal(err)
	}
	after := before
	after.Revision++
	after.State = "SUCCEEDED"
	completion := AgentTasksCompletion{SchemaVersion: 1, SettlementsSHA256: taskSettlementsDigest(markers), MetadataIndex: input.ExpectedIndex, MetadataEpoch: binding.MetadataEpoch, ActionRevision: after.Revision, ExecutionEpoch: after.ExecutionEpoch, WriterFencingToken: control.token, RecordedAt: control.now()}
	after = taskBoundaryField(t, after, agentTasksCompletionField, completion)
	after = taskBoundaryField(t, after, "unrelated", true)
	write, err := prepareControlRecordWrite(after, nil)
	if err != nil {
		t.Fatal(err)
	}
	write.allowBoundLease, write.agentNodeDispatch, write.agentTasksCompletion = true, true, true
	events := countControlEvents(t, control)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	err = control.putControlRecordTx(tx, write, control.now())
	_ = tx.Rollback()
	if !errors.Is(err, ErrAgentRunExecutionRequired) {
		t.Fatalf("terminal write changed preexisting payload under a generic legal transition: %v", err)
	}
	if countControlEvents(t, control) != events {
		t.Fatal("rejected private edge wrote immutable history")
	}
	if _, err := control.CompleteAgentTasks(input); err != nil {
		t.Fatalf("private edge refusal poisoned genuine completion: %v", err)
	}
}

func TestTaskSettlementRejectsMalformedAndReboundPrivateProofMaps(t *testing.T) {
	control, input, _ := taskSettlementFixture(t, false)
	result, err := control.SettleAgentTask(input)
	if err != nil {
		t.Fatal(err)
	}
	binding, err := decodeAgentExecution(result.Action)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"invalid-json", "null-markers", "empty-markers", "unknown-marker-field", "invalid-binding", "missing-binding", "bad-dispatch", "bad-outcome"} {
		t.Run(name, func(t *testing.T) {
			record := result.Action
			switch name {
			case "invalid-json":
				record.Payload = json.RawMessage(`{`)
			case "null-markers":
				record = taskBoundaryField(t, record, agentTaskSettlementField, json.RawMessage(`null`))
			case "empty-markers":
				record = taskBoundaryField(t, record, agentTaskSettlementField, json.RawMessage(`{}`))
			case "unknown-marker-field":
				record = taskBoundaryField(t, record, agentTaskSettlementField, json.RawMessage(`{"coder":{"unexpected":true}}`))
			case "invalid-binding":
				changed := binding
				changed.PlanDigest = "invalid"
				record = taskBoundaryField(t, record, "nativeAgentExecution", changed)
			case "missing-binding":
				record = taskBoundaryField(t, record, "nativeAgentExecution", nil)
			case "bad-dispatch":
				record = taskBoundaryField(t, record, agentNodeDispatchField, json.RawMessage(`[]`))
			case "bad-outcome":
				record = taskBoundaryField(t, record, agentNodeOutcomeField, json.RawMessage(`[]`))
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			_, err = control.validatedAgentTaskSettlementsTx(tx, record)
			_ = tx.Rollback()
			if err == nil {
				t.Fatal("malformed or rebound private proof map was accepted")
			}
		})
	}
	if replay, err := control.SettleAgentTask(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("invalid private map damaged original settlement: %v", err)
	}
}

func TestTaskSettlementRechecksOriginalAndCompletedImmutableMetadata(t *testing.T) {
	for _, name := range []string{"original-missing", "original-request", "original-plan", "original-epoch", "completed-missing", "completed-request", "completed-plan", "completed-counters", "settled-action-missing", "settled-action-epoch", "admission-missing"} {
		t.Run(name, func(t *testing.T) {
			control, input, _ := taskSettlementFixture(t, false)
			result, err := control.SettleAgentTask(input)
			if err != nil {
				t.Fatal(err)
			}
			binding, err := decodeAgentExecution(result.Action)
			if err != nil {
				t.Fatal(err)
			}
			domain, revision := "agent_run", binding.MetadataIndex
			if strings.HasPrefix(name, "completed-") {
				revision++
			}
			if strings.HasPrefix(name, "settled-action-") {
				domain, revision = "action", result.Action.Revision
			}
			var change func(*Record)
			if !strings.HasSuffix(name, "missing") {
				change = func(record *Record) {
					if strings.HasSuffix(name, "epoch") {
						record.ExecutionEpoch++
						return
					}
					var run agent.RunMetadata
					if err := json.Unmarshal(record.Payload, &run); err != nil {
						t.Fatal(err)
					}
					switch {
					case strings.HasSuffix(name, "request"):
						run.Request.SHA256 = strings.Repeat("9", 64)
					case strings.HasSuffix(name, "plan"):
						run.Plan[0].Task = "rebound task"
					case strings.HasSuffix(name, "counters"):
						run.LastEvent.PromptTokens++
					}
					record.Payload, err = json.Marshal(run)
					if err != nil {
						t.Fatal(err)
					}
				}
			}
			var restore func()
			if name == "admission-missing" {
				if _, err := control.db.Exec("CREATE TEMP VIEW action_lease_events AS SELECT * FROM main.action_lease_events WHERE event_type<>'ADMITTED'"); err != nil {
					t.Fatal(err)
				}
				restore = func() {
					if _, err := control.db.Exec("DROP VIEW temp.action_lease_events"); err != nil {
						t.Fatal(err)
					}
				}
			} else {
				id := input.RunID
				if domain == "action" {
					id = input.ActionID
				}
				restore = taskBoundaryHistoryView(t, control, domain, id, revision, change)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			_, err = control.validatedAgentTaskSettlementsTx(tx, result.Action)
			_ = tx.Rollback()
			restore()
			if !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("rebound or absent %s checkpoint passed: %v", name, err)
			}
			if replay, err := control.SettleAgentTask(input); err != nil || !replay.AlreadyApplied {
				t.Fatalf("history fault damaged original proof: %v", err)
			}
		})
	}
}

func TestTaskAPIsRefuseLostAuthorityAndMissingReservationsWithoutWrites(t *testing.T) {
	for _, phase := range []string{"settlement", "completion"} {
		for _, name := range []string{"invalid-id", "zero-epoch", "overflow-epoch", "invalid-cursor", "overflow-cursor", "missing-owner", "missing-claim", "missing-actor", "missing-operation", "missing-action", "missing-action-authority", "corrupt-run", "missing-reservation"} {
			t.Run(phase+"/"+name, func(t *testing.T) {
				var control *Control
				var settle AgentTaskSettlementRequest
				var complete CompleteAgentTasksRequest
				if phase == "settlement" {
					control, settle, _ = taskSettlementFixture(t, false)
					complete = CompleteAgentTasksRequest{OperationID: settle.OperationID, Actor: settle.Actor, ActionID: settle.ActionID, RunID: settle.RunID, Owner: settle.Owner, ClaimToken: settle.ClaimToken, Epoch: settle.Epoch, ExpectedIndex: settle.ExpectedIndex}
				} else {
					control, complete = tasksCompletionFixture(t, false)
				}
				original := complete
				before, _, err := control.Get("action", original.ActionID)
				if err != nil {
					t.Fatal(err)
				}
				events := countControlEvents(t, control)
				var restore func()
				switch name {
				case "invalid-id":
					complete.ActionID = ""
				case "zero-epoch":
					complete.Epoch = 0
				case "overflow-epoch":
					complete.Epoch = math.MaxUint64
				case "invalid-cursor":
					complete.ExpectedIndex = 0
				case "overflow-cursor":
					complete.ExpectedIndex = math.MaxInt64
				case "missing-owner":
					complete.Owner = ""
				case "missing-claim":
					complete.ClaimToken = ""
				case "missing-actor":
					complete.Actor = ""
				case "missing-operation":
					complete.OperationID = ""
				case "missing-action":
					complete.ActionID = "unknown-action"
				case "missing-action-authority", "missing-reservation":
					table, condition := "control_cutover", "domain<>'action'"
					if name == "missing-reservation" {
						table, condition = "action_resource_leases", "0"
					}
					if _, err := control.db.Exec("CREATE TEMP VIEW " + table + " AS SELECT * FROM main." + table + " WHERE " + condition); err != nil {
						t.Fatal(err)
					}
					restore = func() {
						if _, err := control.db.Exec("DROP VIEW temp." + table); err != nil {
							t.Fatal(err)
						}
					}
				case "corrupt-run":
					record, _, err := control.Get("agent_run", original.RunID)
					if err != nil {
						t.Fatal(err)
					}
					var run agent.RunMetadata
					if err := json.Unmarshal(record.Payload, &run); err != nil {
						t.Fatal(err)
					}
					run.Request.Length = 0
					record.Payload, _ = json.Marshal(run)
					restore = settlementRunReadOverride(t, control, record)
				}
				if phase == "settlement" {
					settle.OperationID, settle.Actor, settle.ActionID, settle.RunID, settle.Owner, settle.ClaimToken, settle.Epoch, settle.ExpectedIndex = complete.OperationID, complete.Actor, complete.ActionID, complete.RunID, complete.Owner, complete.ClaimToken, complete.Epoch, complete.ExpectedIndex
					_, err = control.SettleAgentTask(settle)
				} else {
					_, err = control.CompleteAgentTasks(complete)
				}
				if restore != nil {
					restore()
				}
				if err == nil {
					t.Fatal("unavailable authority or scope completed an effect")
				}
				after, _, readErr := control.Get("action", original.ActionID)
				resources, resourceErr := control.GetResourceLeases(original.ActionID)
				if readErr != nil || resourceErr != nil || len(resources) != 1 || countControlEvents(t, control) != events || after.Revision != before.Revision || !bytes.Equal(after.Payload, before.Payload) {
					t.Fatalf("rejected request changed phase authority: %v %v", readErr, resourceErr)
				}
				if _, found, err := control.ReadOperatorMutation(original.OperationID); err != nil || found {
					t.Fatal("rejected request left an operator receipt")
				}
			})
		}
	}
}

func TestTaskTransactionsRollbackLateSQLFailuresAndPermitRecovery(t *testing.T) {
	for _, phase := range []string{"settlement", "completion"} {
		faults := []string{"operator-receipt", "writer-at-commit"}
		if phase == "completion" {
			faults = append(faults, "terminal-lease", "release-resources")
		}
		for _, name := range faults {
			t.Run(phase+"/"+name, func(t *testing.T) {
				var control *Control
				var input CompleteAgentTasksRequest
				var task AgentTaskSettlementRequest
				if phase == "completion" {
					control, input = tasksCompletionFixture(t, false)
				} else {
					control, task, _ = taskSettlementFixture(t, false)
					input = CompleteAgentTasksRequest{ActionID: task.ActionID, RunID: task.RunID, OperationID: task.OperationID}
				}
				before, _, err := control.Get("action", input.ActionID)
				if err != nil {
					t.Fatal(err)
				}
				events := countControlEvents(t, control)
				table, operation := operatorMutationTable, "INSERT"
				if name == "terminal-lease" {
					table, operation = "action_leases", "UPDATE"
				}
				if name == "release-resources" {
					table, operation = "action_resource_leases", "DELETE"
				}
				fault := fmt.Sprintf("CREATE TEMP TRIGGER task_late_fault BEFORE %s ON %s BEGIN SELECT RAISE(ABORT,'task late failure'); END", operation, table)
				if name == "writer-at-commit" {
					fault = "CREATE TEMP TRIGGER task_late_fault AFTER INSERT ON " + operatorMutationTable + " BEGIN UPDATE control_writer SET fencing_token=fencing_token+1 WHERE singleton=1; END"
				}
				if _, err := control.db.Exec(fault); err != nil {
					t.Fatal(err)
				}
				if phase == "completion" {
					_, err = control.CompleteAgentTasks(input)
				} else {
					_, err = control.SettleAgentTask(task)
				}
				if err == nil {
					t.Fatal("late SQL failure reported a completed phase")
				}
				if _, err := control.db.Exec("DROP TRIGGER task_late_fault"); err != nil {
					t.Fatal(err)
				}
				after, _, err := control.Get("action", input.ActionID)
				resources, resourceErr := control.GetResourceLeases(input.ActionID)
				if err != nil || resourceErr != nil || len(resources) != 1 || countControlEvents(t, control) != events || after.Revision != before.Revision || !bytes.Equal(after.Payload, before.Payload) {
					t.Fatalf("late SQL failure left partial state: %v %v", err, resourceErr)
				}
				if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
					t.Fatal("rolled back phase acquired replay receipt")
				}
				if phase == "completion" {
					_, err = control.CompleteAgentTasks(input)
				} else {
					_, err = control.SettleAgentTask(task)
				}
				if err != nil {
					t.Fatalf("rolled back failure poisoned recovery: %v", err)
				}
			})
		}
	}
}

func TestTasksCompletionRechecksDigestStatusAndTerminalLeaseProof(t *testing.T) {
	control, input := tasksCompletionFixture(t, false)
	result, err := control.CompleteAgentTasks(input)
	if err != nil {
		t.Fatal(err)
	}
	completion, present, err := readAgentTasksCompletion(result.Action)
	if err != nil || !present {
		t.Fatal(err)
	}
	for _, name := range []string{"malformed-payload", "uppercase-digest", "nonhex-digest", "missing-lease", "wrong-terminal-state", "wrong-terminal-writer"} {
		t.Run(name, func(t *testing.T) {
			record := result.Action
			var restore func()
			switch name {
			case "malformed-payload":
				record.Payload = json.RawMessage(`{`)
			case "uppercase-digest":
				changed := completion
				changed.SettlementsSHA256 = strings.Repeat("A", 64)
				record = taskBoundaryField(t, record, agentTasksCompletionField, changed)
			case "nonhex-digest":
				changed := completion
				changed.SettlementsSHA256 = strings.Repeat("z", 64)
				record = taskBoundaryField(t, record, agentTasksCompletionField, changed)
			default:
				query := "CREATE TEMP VIEW action_leases AS SELECT * FROM main.action_leases WHERE 0"
				if name != "missing-lease" {
					terminal, writer := "terminal_state", "writer_fencing_token"
					if name == "wrong-terminal-state" {
						terminal = "'FAILED_BEFORE_EFFECT'"
					} else {
						writer = "writer_fencing_token+1"
					}
					query = fmt.Sprintf(`CREATE TEMP VIEW action_leases AS SELECT action_id,owner,epoch,claim_token,lease_until,claim_revision,acquired_at,%s AS terminal_state,%s AS writer_fencing_token,updated_at FROM main.action_leases`, terminal, writer)
				}
				if _, err := control.db.Exec(query); err != nil {
					t.Fatal(err)
				}
				restore = func() {
					if _, err := control.db.Exec("DROP VIEW temp.action_leases"); err != nil {
						t.Fatal(err)
					}
				}
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentTasksCompletionTx(tx, record)
			_ = tx.Rollback()
			if restore != nil {
				restore()
			}
			if !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("corrupt %s completed a phase: %v", name, err)
			}
		})
	}
	if replay, err := control.CompleteAgentTasks(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("terminal proof rejection damaged replay: %v", err)
	}
}

func TestTaskPhaseReplayCannotBindAnotherAction(t *testing.T) {
	for _, phase := range []string{"settlement", "completion"} {
		t.Run(phase, func(t *testing.T) {
			var control *Control
			var task AgentTaskSettlementRequest
			var input CompleteAgentTasksRequest
			var err error
			if phase == "completion" {
				control, input = tasksCompletionFixture(t, false)
				_, err = control.CompleteAgentTasks(input)
			} else {
				control, task, _ = taskSettlementFixture(t, false)
				_, err = control.SettleAgentTask(task)
				input.OperationID = task.OperationID
			}
			if err != nil {
				t.Fatal(err)
			}
			query := `CREATE TEMP VIEW control_operator_mutations AS SELECT operation_id,domain,'another-action' AS record_id,record_revision,record_state,payload_digest,request_digest,action_id,execution_epoch,cutover_revision,fencing_token,writer_fencing_token,actor,recorded_at FROM main.control_operator_mutations`
			if _, err := control.db.Exec(query); err != nil {
				t.Fatal(err)
			}
			if phase == "completion" {
				_, err = control.CompleteAgentTasks(input)
			} else {
				_, err = control.SettleAgentTask(task)
			}
			if !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("rebound replay receipt was accepted: %v", err)
			}
			if _, err := control.db.Exec("DROP VIEW temp.control_operator_mutations"); err != nil {
				t.Fatal(err)
			}
			if phase == "completion" {
				result, err := control.CompleteAgentTasks(input)
				if err != nil || !result.AlreadyApplied {
					t.Fatalf("valid phase replay lost: %v", err)
				}
			} else {
				result, err := control.SettleAgentTask(task)
				if err != nil || !result.AlreadyApplied {
					t.Fatalf("valid task replay lost: %v", err)
				}
			}
		})
	}
}

func TestTaskCompletionRefusesCancelledMetadataAndClosedWriter(t *testing.T) {
	for _, name := range []string{"cancelled", "closed-writer"} {
		t.Run(name, func(t *testing.T) {
			control, input := tasksCompletionFixture(t, false)
			if name == "closed-writer" {
				if err := control.Close(); err != nil {
					t.Fatal(err)
				}
				if _, err := control.CompleteAgentTasks(input); !errors.Is(err, ErrWriterFenceHeld) {
					t.Fatalf("closed writer completed phase: %v", err)
				}
				return
			}
			record, _, err := control.Get("agent_run", input.RunID)
			if err != nil {
				t.Fatal(err)
			}
			var run agent.RunMetadata
			if err := json.Unmarshal(record.Payload, &run); err != nil {
				t.Fatal(err)
			}
			run.Cancelled = true
			record.Payload, _ = json.Marshal(run)
			restore := settlementRunReadOverride(t, control, record)
			_, err = control.CompleteAgentTasks(input)
			restore()
			if err == nil {
				t.Fatal("cancelled metadata released phase authority")
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatal("cancelled phase lost reservations")
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatal("cancelled phase acquired completion receipt")
			}
		})
	}
}
