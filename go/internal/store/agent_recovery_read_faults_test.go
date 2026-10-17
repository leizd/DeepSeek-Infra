package store

import (
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

// Read-fault boundaries the slice left open: scan failures on shadowed event
// payloads, an undecodable retained content row and a dropped reset source.

func TestRecoveryAndEventReadsSurfaceScanFailures(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`CREATE TEMP VIEW control_events AS SELECT event_id, domain, record_id, revision, execution_epoch, state,
        NULL AS payload_json, record_digest, writer_fencing_token, recorded_at FROM main.control_events`); err != nil {
		t.Fatal(err)
	}
	defer func() { _, _ = control.db.Exec("DROP VIEW control_events") }()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := recoveryReplayEventsTx(tx, run.RunID, 1, 5); err == nil {
		_ = tx.Rollback()
		t.Fatal("a NULL payload replay scan was hidden")
	}
	_ = tx.Rollback()
	if _, err := control.AgentRunEventsAfter(run.RunID, 0, 1024); err == nil {
		t.Fatal("a NULL payload event scan was hidden")
	}
}

func TestRecoveryPreviewSurfacesAnUndecodableContentRow(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`INSERT INTO control_events(domain, record_id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, recorded_at)
        VALUES('agent_run', ?, ?, 1, 'running', ?, ?, 1, 0)`, run.RunID, run.NextIndex+70,
		`{"schemaVersion":9,"lastEvent":{"type":"content","index":9999},"status":"running"}`, strings.Repeat("d", 64)); err != nil {
		t.Fatal(err)
	}
	if _, err := control.PreviewAgentRunRecovery(input.AgentRunRecoveryExpected); err == nil {
		t.Fatal("an undecodable content row was accepted as final presence")
	}
}

func TestRecoveryPreviewSurfacesARetainedResetReadFailure(t *testing.T) {
	control, runID := recoveryCompletedRun(t)
	expected := recoveryExpectedFromRun(t, control, runID, &agentv1.AgentRunRecoverySelection{
		Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME})
	if _, err := control.db.Exec("DROP TABLE control_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.PreviewAgentRunRecovery(expected); err == nil {
		t.Fatal("a dropped retained-event source was hidden")
	}
}
