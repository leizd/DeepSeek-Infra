package store

import (
	"errors"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

// Remaining recovery/event boundaries: direct validator calls, dropped tables,
// crafted immutable event rows and public refusals that the slice left open.

func TestRecoveryHelpersRefuseUndecodableInput(t *testing.T) {
	if !changedAgentRecoveryScope(Record{Payload: []byte("{")}, Record{Payload: []byte("null")}) {
		t.Fatal("an undecodable predecessor was treated as unchanged")
	}
	if nativeAgentMetadata([]byte("{")) {
		t.Fatal("undecodable payload was treated as native agent metadata")
	}
}

func TestRecoveryHistoryTableFaultsAreSurfaced(t *testing.T) {
	t.Run("reservations", func(t *testing.T) {
		control, input := recoveryAdmissionFixture(t, "failed")
		run, found, err := control.AgentRunMetadata(input.RunID)
		if err != nil || !found {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("DROP TABLE action_resource_leases"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback()
		if _, err := control.agentRecoveryHistoryTx(tx, run, 0); err == nil {
			t.Fatal("a dropped reservation table was hidden")
		}
	})
	t.Run("journal", func(t *testing.T) {
		control, input := recoveryAdmissionFixture(t, "failed")
		run, found, err := control.AgentRunMetadata(input.RunID)
		if err != nil || !found {
			t.Fatal(err)
		}
		if _, err := control.db.Exec("DROP TABLE action_journal"); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback()
		if _, err := control.agentRecoveryHistoryTx(tx, run, 0); err == nil {
			t.Fatal("a dropped journal table was hidden")
		}
	})
}

func TestRecoveryReplayRefusesFinishedAndCraftedRows(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	finished, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := finished.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, err := recoveryReplayEventsTx(finished, run.RunID, 1, 5); err == nil {
		t.Fatal("a finished transaction was reused for replay")
	}
	crafted := run.NextIndex + 50
	if _, err := control.db.Exec(`INSERT INTO control_events(domain, record_id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, recorded_at)
        VALUES('agent_run', ?, ?, 1, 'running', ?, ?, 1, 0)`, run.RunID, crafted, `{"lastEvent":{"index":999}}`, strings.Repeat("a", 64)); err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := recoveryReplayEventsTx(tx, run.RunID, crafted, crafted+1); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("a crafted replay row was accepted: %v", err)
	}
}

func TestAgentRunEventsAfterRefusesUnknownRunsAndCraftedRows(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	if _, err := control.AgentRunEventsAfter("run_missing_events_boundary", 0, 10); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("an unknown run was paged: %v", err)
	}
	crafted := run.NextIndex + 60
	if _, err := control.db.Exec(`INSERT INTO control_events(domain, record_id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, recorded_at)
        VALUES('agent_run', ?, ?, 1, 'running', ?, ?, 1, 0)`, run.RunID, crafted, `{"lastEvent":"not-an-object"}`, strings.Repeat("b", 64)); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AgentRunEventsAfter(run.RunID, 0, 1024); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("a crafted event payload was accepted: %v", err)
	}
}

func TestRecoveryFinalPresenceRefusesAnUnmatchedHistory(t *testing.T) {
	control, runID := recoveryCompletedRun(t)
	run, found, err := control.AgentRunMetadata(runID)
	if err != nil || !found {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	present, err := recoveryFinalPresentTx(tx, run, agentRecoveryHistory{})
	if err != nil || present {
		t.Fatalf("an unmatched synthesis history claimed a final answer: %v %v", present, err)
	}
}

func TestAgentRunEventsRefuseCreateOnExistingAndRuntimeSwapOnAScope(t *testing.T) {
	control, confirmation := runtimeUpdateFixture(t, false)
	confirmed, err := control.AppendAgentRunEvent(confirmation)
	if err != nil {
		t.Fatal(err)
	}
	extra := eventMutation(t, control, confirmed.Run.NextIndex, agent.RunEvent{Type: "agent_note", Phase: "coder"})
	extra.Create = &agent.RunMetadata{Request: agent.ArtifactRef{SHA256: strings.Repeat("c", 64), Length: 10}}
	if _, err := control.AppendAgentRunEvent(extra); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("create metadata was accepted on an existing run: %v", err)
	}
	scoped, input := recoveryAdmissionFixture(t, "failed")
	result, err := scoped.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	swap := eventMutation(t, scoped, result.Run.NextIndex, agent.RunEvent{Type: "agent_note", Phase: "coder"})
	swap.RuntimeUpdate = &AgentRunRuntimeUpdate{}
	if _, err := scoped.AppendAgentRunEvent(swap); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("a scope accepted a legacy runtime swap: %v", err)
	}
}
