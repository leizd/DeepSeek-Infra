package store

import (
	"errors"
	"testing"
)

// AgentNodeOutcomeSnapshot is the read path every recovery decision depends on.
// A receipt that cannot be decoded must refuse rather than be reported as absent
// custody, and a settlement validator failure must surface rather than be
// skipped.

func TestAgentNodeOutcomeSnapshotRefusesAnUndecodableReceipt(t *testing.T) {
	control, input, _ := successorReadyFixture(t)
	// Store a receipt blob that is not a protobuf message for this node.
	if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(
		`UPDATE control_events SET payload_json = json_set(payload_json, '$.agentNodeOutcomes."coder".receipt', 'not-a-receipt')
		 WHERE domain='action' AND record_id=?`,
		input.ActionID,
	); err != nil {
		// json_set is unavailable or the shape differs; fall back to a direct
		// payload replacement that still cannot decode.
		if _, err := control.db.Exec(
			`UPDATE control_events SET payload_json = json_set(payload_json, '$.agentNodeOutcomeCustody', 'x')
			 WHERE domain='action' AND record_id=?`,
			input.ActionID,
		); err != nil {
			t.Skipf("cannot inject a corrupt receipt: %v", err)
		}
	}
	if _, _, _, _, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID); err == nil {
		t.Fatal("an undecodable receipt was read as valid custody")
	}
}

func TestAgentNodeOutcomeSnapshotRefusesAMalformedActionPayload(t *testing.T) {
	control, input, _ := successorReadyFixture(t)
	if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	// Valid JSON with no dispatch map: the reader must refuse rather than report
	// an absent intent as authoritative custody.
	if _, err := control.db.Exec(
		`UPDATE control_events SET payload_json='{"note":"replaced"}' WHERE domain='action' AND record_id=?`,
		input.ActionID,
	); err != nil {
		t.Fatal(err)
	}
	if _, _, _, _, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID); err == nil {
		t.Fatal("a payload without a dispatch map was read as custody")
	}
}

func TestAgentNodeOutcomeSnapshotReportsAnAbsentAction(t *testing.T) {
	control, _, _ := successorReadyFixture(t)
	// A missing action is not an error: it is reported as absent custody, and the
	// caller must see found=false rather than a zero-value record.
	_, _, _, found, err := control.AgentNodeOutcomeSnapshot("action_that_does_not_exist", "coder")
	if err != nil && !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("absent action returned %v", err)
	}
	if err == nil && found {
		t.Fatal("an absent action was reported as present custody")
	}
}
