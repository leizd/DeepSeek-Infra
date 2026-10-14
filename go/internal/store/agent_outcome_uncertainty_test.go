package store

import (
	"testing"

	"google.golang.org/protobuf/proto"
)

func TestAgentOutcomeSurvivesGenericUncertaintyWithoutPhaseSettlement(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	beforeRun, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatalf("missing admitted run: %v", err)
	}
	if _, err := control.RecordAgentNodeOutcome(input); err != nil {
		t.Fatal(err)
	}
	lease, found, err := control.GetActionLease(input.ActionID)
	if err != nil || !found {
		t.Fatalf("missing original live lease: %v", err)
	}
	record, err := control.MarkActionEffectUnknown(input.ActionID, input.Epoch, lease.ClaimToken)
	if err != nil || record.State != "EFFECT_UNKNOWN" {
		t.Fatalf("uncertainty transition lost retained proof: %v", err)
	}
	current, intent, receipt, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || current.State != "EFFECT_UNKNOWN" || !proto.Equal(receipt, input.Receipt) || intent.ExecutionEpoch != input.Epoch {
		t.Fatalf("uncertainty changed historical source proof: %v", err)
	}
	afterRun, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found || afterRun.NextIndex != beforeRun.NextIndex || afterRun.Status != beforeRun.Status {
		t.Fatalf("proof retention prematurely settled run: %v", err)
	}
	var resources, terminations int
	if err := control.db.QueryRow(`SELECT COUNT(*) FROM action_resource_leases WHERE action_id=?`, input.ActionID).Scan(&resources); err != nil {
		t.Fatal(err)
	}
	if err := control.db.QueryRow(`SELECT COUNT(*) FROM action_lease_events WHERE action_id=? AND event_type='TERMINATED'`, input.ActionID).Scan(&terminations); err != nil {
		t.Fatal(err)
	}
	if resources == 0 || terminations != 0 {
		t.Fatal("uncertainty released the original reservation")
	}
	replay, err := control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{OperationID: "uncertain-node-replay", Actor: input.Actor,
		ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID, Owner: lease.Owner, ClaimToken: lease.ClaimToken})
	if err == nil && replay.Fresh {
		t.Fatal("retained result authorized another provider effect")
	}
}
