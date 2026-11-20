package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func TestRecoveryTerminalLabelsNeverReplaceTypedSettlementProof(t *testing.T) {
	for _, row := range []struct{ state, phase, payload string }{
		{"SUCCEEDED", "plan", "{"}, {"CANCELLED_CONFIRMED", "tasks", "{}"},
		{"FAILED", "tasks", "{}"}, {"EFFECT_UNKNOWN", "tasks", "{}"},
		{"SUCCEEDED", "unknown", "{}"}, {"SUCCEEDED", "plan", "{}"},
		{"SUCCEEDED", "tasks", "{}"}, {"SUCCEEDED", "synthesis", "{}"},
	} {
		t.Run(row.state+"/"+row.phase+"/"+row.payload, func(t *testing.T) {
			control := &Control{}
			if err := control.validateAgentRecoveryTerminalTx(nil, Record{State: row.state, Payload: json.RawMessage(row.payload)}, row.phase); err == nil {
				t.Fatal("terminal text without a typed proof authorized recovery")
			}
		})
	}
}

func TestRecoveryHistoryReadFailuresCannotBecomeEmptySuccessfulAudits(t *testing.T) {
	for _, fault := range []string{"run_read", "reservation_read", "action_read", "null_action_id", "corrupt_action"} {
		t.Run(fault, func(t *testing.T) {
			control, input := recoveryAdmissionFixture(t, "cancelled")
			count := countControlEvents(t, control)
			var view, statement string
			switch fault {
			case "run_read":
				view, statement = "agent_runs", "CREATE TEMP VIEW agent_runs AS SELECT id FROM main.agent_runs"
			case "reservation_read":
				view, statement = "action_resource_leases", "CREATE TEMP VIEW action_resource_leases AS SELECT action_id FROM main.action_resource_leases"
			case "action_read":
				view, statement = "action_journal", "CREATE TEMP VIEW action_journal AS SELECT id FROM main.action_journal"
			case "null_action_id":
				view, statement = "action_journal", fmt.Sprintf(`CREATE TEMP VIEW action_journal AS SELECT NULL AS id,
                    json_object('nativeAgentExecution',json_object('runId','%s','metadataIndex',1)) AS payload_json`, input.RunID)
			case "corrupt_action":
				view, statement = "action_journal", fmt.Sprintf(`CREATE TEMP VIEW action_journal AS SELECT 'corrupt-action' AS id,
                    1 AS revision,1 AS execution_epoch,'SUCCEEDED' AS state,
                    json_object('nativeAgentExecution',json_object('runId','%s','metadataIndex',1)) AS payload_json,
                    '' AS record_digest,1 AS writer_fencing_token,1 AS updated_at`, input.RunID)
			}
			recoveryBoundaryExec(t, control, statement)
			_, previewError := control.PreviewAgentRunRecovery(input.AgentRunRecoveryExpected)
			_, commitError := control.CommitAgentRunRecovery(input)
			recoveryBoundaryExec(t, control, "DROP VIEW temp."+view)
			if previewError == nil || commitError == nil || countControlEvents(t, control) != count {
				t.Fatal("unreadable durable history granted a fresh generation", previewError, commitError)
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatal("failed history audit produced an operator receipt", err)
			}
		})
	}
}

func TestRecoveryHistoryBoundsAndMissingReservationsDoNotEraseUnsettledEffects(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	count := countControlEvents(t, control)
	recoveryBoundaryExec(t, control, "DELETE FROM action_resource_leases")
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	for _, cursor := range []int64{-1, run.NextIndex + 1} {
		if _, err := control.agentRecoveryHistoryTx(tx, run, cursor); !errors.Is(err, ErrAgentRunMetadataInvalid) {
			t.Fatal("invalid history cursor hid old effects", err)
		}
	}
	if _, err := control.agentRecoveryHistoryTx(tx, run, 0); err == nil {
		t.Fatal("removing a reservation substituted for confirmed effect settlement")
	}
	_ = tx.Rollback()
	if countControlEvents(t, control) != count {
		t.Fatal("failed effect audit changed public history")
	}
}

func TestRecoveryHistoryRequiresTheOriginalPublicTaskSettlementsAndCursor(t *testing.T) {
	control, runID := recoveryCompletedRun(t)
	run, _, err := control.AgentRunMetadata(runID)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	history, err := control.agentRecoveryHistoryTx(tx, run, 0)
	_ = tx.Rollback()
	if err != nil || len(history.outputs) < 2 {
		t.Fatal("fixture lacks verified original task outputs", err)
	}
	var lastTask int64
	for _, source := range history.outputs {
		if source.SettlementMetadataIndex > lastTask {
			lastTask = source.SettlementMetadataIndex
		}
	}
	truncated := run
	truncated.NextIndex = lastTask
	tx, err = control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	_, rejected := control.agentRecoveryHistoryTx(tx, truncated, 0)
	_ = tx.Rollback()
	if rejected == nil {
		t.Fatal("future task output was accepted behind an earlier public cursor")
	}
	recoveryBoundaryExec(t, control, "CREATE TEMP VIEW control_events AS SELECT * FROM main.control_events WHERE domain<>'agent_run'")
	tx, err = control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	_, rejected = control.agentRecoveryHistoryTx(tx, run, 0)
	_ = tx.Rollback()
	recoveryBoundaryExec(t, control, "DROP VIEW temp.control_events")
	if rejected == nil {
		t.Fatal("Rust custody alone recreated missing public task settlements")
	}
}

func TestRecoveryFinalAnswerMustBelongToTheOriginalSynthesisProof(t *testing.T) {
	control, runID := recoveryCompletedRun(t)
	run, _, err := control.AgentRunMetadata(runID)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	history, err := control.agentRecoveryHistoryTx(tx, run, 0)
	_ = tx.Rollback()
	if err != nil || len(history.synthesis) != 1 {
		t.Fatal("fixture lacks its original synthesis proof", err)
	}
	for _, change := range []string{"missing", "binding", "cursor", "dispatch", "outcomes", "receipt"} {
		t.Run(change, func(t *testing.T) {
			candidate := history
			candidate.synthesis = append([]Record{}, history.synthesis...)
			record := candidate.synthesis[0]
			var fields map[string]json.RawMessage
			if json.Unmarshal(record.Payload, &fields) != nil {
				t.Fatal("cannot detach proof fixture")
			}
			switch change {
			case "missing":
				candidate.synthesis = nil
			case "binding":
				fields["nativeAgentExecution"] = json.RawMessage(`{}`)
			case "cursor":
				binding, err := decodeAgentExecution(record)
				if err != nil {
					t.Fatal(err)
				}
				binding.MetadataIndex++
				record.ID = executionActionID(binding)
				fields["nativeAgentExecution"], _ = json.Marshal(binding)
			case "dispatch":
				fields[agentNodeDispatchField] = json.RawMessage(`[]`)
			case "outcomes":
				fields[agentNodeOutcomeField] = json.RawMessage(`[]`)
			case "receipt":
				outcomes, err := agentNodeOutcomeBytes(record)
				if err != nil {
					t.Fatal(err)
				}
				value := outcomes["synthesizer"]
				value.Receipt, _ = proto.Marshal(&agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1})
				outcomes["synthesizer"] = value
				fields[agentNodeOutcomeField], _ = json.Marshal(outcomes)
			}
			if change != "missing" {
				record.Payload, _ = json.Marshal(fields)
				candidate.synthesis[0] = record
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			present, _ := recoveryFinalPresentTx(tx, run, candidate)
			_ = tx.Rollback()
			if present {
				t.Fatal("unmatched or corrupt synthesis proof preserved a final answer")
			}
		})
	}
	tx, err = control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	_ = tx.Rollback()
	if present, err := recoveryFinalPresentTx(tx, run, history); present || err == nil {
		t.Fatal("unavailable final history was treated as a verified answer", err)
	}
}

func TestRecoveryContextStillChecksItsCallerSnapshotAndSelection(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	run, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := control.agentRecoveryContextTx(tx, Record{Payload: json.RawMessage(`{}`)}, input.AgentRunRecoveryExpected); err == nil {
		t.Fatal("unreadable caller snapshot acquired a context")
	}
	input.Selection.Kind = agentv1.AgentRunRecoveryKind(99)
	if _, err := control.agentRecoveryContextTx(tx, recoveryBoundaryRecord(t, run), input.AgentRunRecoveryExpected); err == nil {
		t.Fatal("unsupported caller selection acquired a context")
	}
}

func TestRecoveryResumeCannotReplaceAnAwaitingPlanConfirmation(t *testing.T) {
	control, confirmation := runtimeUpdateFixture(t, false)
	expected := recoveryExpectedFromRun(t, control, confirmation.RunID,
		&agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME})
	count := countControlEvents(t, control)
	if _, err := control.PreviewAgentRunRecovery(expected); !errors.Is(err, ErrIllegalTransition) || countControlEvents(t, control) != count {
		t.Fatal("resume bypassed explicit public plan confirmation", err)
	}
}
