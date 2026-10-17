package store

import (
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

// Final refusal boundaries: shadowed journal projections, a poisoned finish
// barrier and a dropped agent account table. Each probe asserts the refusal.

func TestRecoveryHistoryRefusesAnUnscannableJournalID(t *testing.T) {
	control, runID := recoveryCompletedRun(t)
	run, found, err := control.AgentRunMetadata(runID)
	if err != nil || !found {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`CREATE TEMP VIEW action_journal AS SELECT NULL AS id, revision, execution_epoch, state, payload_json,
        record_digest, writer_fencing_token, updated_at FROM main.action_journal`); err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = control.db.Exec("DROP VIEW action_journal") }()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := control.agentRecoveryHistoryTx(tx, run, 0); err == nil {
		t.Fatal("an unscannable journal id was hidden")
	}
}

func TestRecoveryHistoryRefusesTamperedJournalRows(t *testing.T) {
	for _, testCase := range []struct{ name, columns, from string }{
		{"binding", "json_set(payload_json,'$.nativeAgentExecution.metadataIndex',9999) AS payload_json", "revision, execution_epoch, state"},
		{"terminal", "payload_json", "revision, execution_epoch, 'CANCELLED_CONFIRMED' AS state"},
		{"dispatches", "json_set(payload_json,'$.nativeAgentNodeDispatches','broken') AS payload_json", "revision, execution_epoch, state"},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			control, runID := recoveryCompletedRun(t)
			run, found, err := control.AgentRunMetadata(runID)
			if err != nil || !found {
				t.Fatal(err)
			}
			view := "CREATE TEMP VIEW action_journal AS SELECT id, " + testCase.columns + ", " + testCase.from +
				", record_digest, writer_fencing_token, updated_at FROM main.action_journal"
			if _, err := control.db.Exec(view); err != nil {
				t.Fatal(err)
			}
			defer func() { _, _ = control.db.Exec("DROP VIEW action_journal") }()
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if _, err := control.agentRecoveryHistoryTx(tx, run, 0); err == nil {
				t.Fatal("a tampered journal row was accepted")
			}
		})
	}
}

func TestRecoveryEventAndCommitBarriersSurfaceFinishFailures(t *testing.T) {
	finishTrigger := func(control *Control, operationID string) {
		t.Helper()
		if _, err := control.db.Exec(`CREATE TRIGGER finish_fault BEFORE UPDATE ON control_writer
            WHEN (SELECT COUNT(*) FROM control_operator_mutations WHERE operation_id = '` + operationID + `') > 0
            BEGIN SELECT RAISE(FAIL,'injected finish failure'); END`); err != nil {
			t.Fatal(err)
		}
	}
	eventMutationFor := func(t *testing.T, control *Control, mutation AgentRunEventMutation) AgentRunEventMutation {
		t.Helper()
		authority, err := control.GetCutover("agent_run")
		if err != nil {
			t.Fatal(err)
		}
		mutation.Fence = &internalprotocol.ActionFence{ActionId: mutation.RunID, ExecutionEpoch: uint64(authority.Epoch)}
		return mutation
	}
	t.Run("event finish", func(t *testing.T) {
		control, confirmation := runtimeUpdateFixture(t, false)
		confirmed, err := control.AppendAgentRunEvent(confirmation)
		if err != nil {
			t.Fatal(err)
		}
		mutation := eventMutationFor(t, control, AgentRunEventMutation{OperationID: "finish-plain", Actor: "operator:boundary-test",
			RunID: confirmed.Run.RunID, ExpectedIndex: confirmed.Run.NextIndex,
			Event: agent.RunEvent{Type: "agent_note", Phase: "coder", Body: agent.ArtifactRef{SHA256: strings.Repeat("a", 64), Length: 30}}})
		finishTrigger(control, mutation.OperationID)
		if _, err := control.AppendAgentRunEvent(mutation); err == nil || !strings.Contains(err.Error(), "injected finish failure") {
			t.Fatalf("a failed event finish was hidden: %v", err)
		}
	})
	t.Run("event replay finish", func(t *testing.T) {
		control, confirmation := runtimeUpdateFixture(t, false)
		confirmed, err := control.AppendAgentRunEvent(confirmation)
		if err != nil {
			t.Fatal(err)
		}
		mutation := eventMutationFor(t, control, AgentRunEventMutation{OperationID: "finish-replay", Actor: "operator:boundary-test",
			RunID: confirmed.Run.RunID, ExpectedIndex: confirmed.Run.NextIndex,
			Event: agent.RunEvent{Type: "agent_note", Phase: "coder", Body: agent.ArtifactRef{SHA256: strings.Repeat("a", 64), Length: 30}}})
		if _, err := control.AppendAgentRunEvent(mutation); err != nil {
			t.Fatal(err)
		}
		finishTrigger(control, mutation.OperationID)
		if _, err := control.AppendAgentRunEvent(mutation); err == nil || !strings.Contains(err.Error(), "injected finish failure") {
			t.Fatalf("a failed replayed finish was hidden: %v", err)
		}
	})
	t.Run("commit finish", func(t *testing.T) {
		control, input := recoveryAdmissionFixture(t, "failed")
		finishTrigger(control, input.OperationID)
		if _, err := control.CommitAgentRunRecovery(input); err == nil || !strings.Contains(err.Error(), "injected finish failure") {
			t.Fatalf("a failed commit finish was hidden: %v", err)
		}
	})
	t.Run("commit replay finish", func(t *testing.T) {
		control, input := recoveryAdmissionFixture(t, "failed")
		if _, err := control.CommitAgentRunRecovery(input); err != nil {
			t.Fatal(err)
		}
		finishTrigger(control, input.OperationID)
		if _, err := control.CommitAgentRunRecovery(input); err == nil || !strings.Contains(err.Error(), "injected finish failure") {
			t.Fatalf("a failed replayed commit finish was hidden: %v", err)
		}
	})
	t.Run("event write", func(t *testing.T) {
		control, confirmation := runtimeUpdateFixture(t, false)
		confirmed, err := control.AppendAgentRunEvent(confirmation)
		if err != nil {
			t.Fatal(err)
		}
		mutation := eventMutationFor(t, control, AgentRunEventMutation{OperationID: "event-write-fault", Actor: "operator:boundary-test",
			RunID: confirmed.Run.RunID, ExpectedIndex: confirmed.Run.NextIndex,
			Event: agent.RunEvent{Type: "agent_note", Phase: "coder", Body: agent.ArtifactRef{SHA256: strings.Repeat("a", 64), Length: 30}}})
		if _, err := control.db.Exec(`CREATE TRIGGER event_write_fault BEFORE INSERT ON control_events
            BEGIN SELECT RAISE(FAIL,'injected event write failure'); END`); err != nil {
			t.Fatal(err)
		}
		if _, err := control.AppendAgentRunEvent(mutation); err == nil || !strings.Contains(err.Error(), "injected event write failure") {
			t.Fatalf("a failed event write was hidden: %v", err)
		}
	})
}

func TestRecoveryCommitSurfacesADroppedAgentRunTable(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	if _, err := control.db.Exec("DROP TABLE agent_runs"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.CommitAgentRunRecovery(input); err == nil {
		t.Fatal("a dropped agent run table was hidden")
	}
}
