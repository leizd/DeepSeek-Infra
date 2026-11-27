package store

import (
	"errors"
	"strings"
	"testing"
)

// Every write step in the dispatch and successor paths must surface a storage
// failure instead of reporting success. Those branches were uncovered: a
// swallowed error there would report a durable intent that was never committed.

func TestDispatchAndSuccessorSurfaceEveryWriteFailure(t *testing.T) {
	// Each case installs one trigger so a specific write fails, then asserts the
	// call refuses. The trigger is removed by the per-test temp database.
	cases := []struct {
		name    string
		trigger string
	}{
		{
			name: "operator-mutation",
			trigger: `CREATE TEMP TRIGGER deny_dispatch_mutation BEFORE INSERT ON control_operator_mutations
				BEGIN SELECT RAISE(ABORT,'injected dispatch mutation failure'); END`,
		},
		{
			name: "control-event",
			trigger: `CREATE TEMP TRIGGER deny_dispatch_event BEFORE INSERT ON control_events
				BEGIN SELECT RAISE(ABORT,'injected dispatch event failure'); END`,
		},
		{
			name: "writer-fence",
			trigger: `CREATE TEMP TRIGGER deny_dispatch_writer BEFORE UPDATE ON control_writer
				BEGIN SELECT RAISE(ABORT,'injected dispatch writer failure'); END`,
		},
	}

	for _, row := range cases {
		t.Run("begin-"+row.name, func(t *testing.T) {
			control, input := agentNodeDispatchFixture(t)
			if _, err := control.db.Exec(row.trigger); err != nil {
				t.Fatalf("install trigger: %v", err)
			}
			if _, err := control.BeginAgentNodeDispatch(input); err == nil {
				t.Fatalf("%s was reported as a durable dispatch", row.name)
			}
		})

		t.Run("bind-"+row.name, func(t *testing.T) {
			control, input, _ := successorReadyFixture(t)
			if _, err := control.db.Exec(row.trigger); err != nil {
				t.Fatalf("install trigger: %v", err)
			}
			if _, err := control.BindAgentNodeSuccessor(successorBindRequest(input)); err == nil {
				t.Fatalf("%s was reported as a durable binding", row.name)
			}
		})
	}
}

func TestAdmitSuccessorDispatchSurfacesStorageFailure(t *testing.T) {
	control, input, _ := successorReadyFixture(t)
	if _, err := control.BindAgentNodeSuccessor(successorBindRequest(input)); err != nil {
		t.Skipf("fixture cannot bind a successor: %v", err)
	}
	if _, err := control.db.Exec(`CREATE TEMP TRIGGER deny_successor_epoch BEFORE INSERT ON control_operator_mutations
		BEGIN SELECT RAISE(ABORT,'injected successor epoch failure'); END`); err != nil {
		t.Fatal(err)
	}
	epoch := AgentSuccessorEpochRequest{
		OperationID: "admit-successor-fault", Actor: input.Actor, ActionID: input.ActionID,
		Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID,
		Owner: input.Owner, ClaimToken: input.ClaimToken, Effect: "CONFIRMED",
		ResetAdmitted: true, ResetFenceMatches: true, ResetIndex: 5,
	}
	if _, err := control.AdmitSuccessorDispatch(epoch); err == nil {
		t.Fatal("a storage failure was reported as an admitted successor")
	}
}

func TestAgentNodeDispatchSnapshotRefusesACorruptPayload(t *testing.T) {
	control, input, _ := successorReadyFixture(t)
	// Replace the stored payload with valid JSON that carries no dispatch map:
	// the schema accepts the bytes, and the reader must still refuse rather than
	// report an empty but authoritative custody view.
	if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(
		`UPDATE control_events SET payload_json='{"note":"replaced"}' WHERE domain='action' AND record_id=?`,
		input.ActionID,
	); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := control.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID); err == nil {
		t.Fatal("a dispatch payload without an intent was read as valid custody")
	}
}

func TestDispatchAndSuccessorRefuseCorruptActionRecords(t *testing.T) {
	control, input, _ := successorReadyFixture(t)
	bind := successorBindRequest(input)
	admit := AgentSuccessorEpochRequest{
		OperationID: "admit-corrupt-successor", Actor: bind.Actor, ActionID: bind.ActionID,
		Epoch: bind.Epoch, RunID: bind.RunID, NodeID: bind.NodeID, Owner: bind.Owner,
		ClaimToken: bind.ClaimToken, Effect: bind.Effect, ResetAdmitted: bind.ResetAdmitted,
		ResetFenceMatches: bind.ResetFenceMatches, ResetIndex: bind.ResetIndex,
	}
	before := countControlEvents(t, control)
	digest := strings.Repeat("0", 64)
	if _, err := control.db.Exec("UPDATE action_journal SET record_digest=? WHERE id=?", digest, input.ActionID); err != nil {
		t.Fatal(err)
	}
	dispatch := input
	dispatch.OperationID = "dispatch-corrupt-successor"
	if _, err := control.BeginAgentNodeDispatch(dispatch); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("dispatch accepted corrupt custody: %v", err)
	}
	if _, err := control.BindAgentNodeSuccessor(bind); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("binding accepted corrupt custody: %v", err)
	}
	if _, err := control.AdmitSuccessorDispatch(admit); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("admission accepted corrupt custody: %v", err)
	}
	var afterDigest string
	if err := control.db.QueryRow("SELECT record_digest FROM action_journal WHERE id=?", input.ActionID).Scan(&afterDigest); err != nil ||
		afterDigest != digest || countControlEvents(t, control) != before {
		t.Fatalf("refusal changed the unreadable action or its history: %v", err)
	}
}
