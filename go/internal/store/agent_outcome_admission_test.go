package store

import (
	"bytes"
	"errors"
	"reflect"
	"testing"

	"google.golang.org/protobuf/proto"
)

func agentLateOutcomeFixture(t *testing.T) (*Control, AgentNodeOutcomeRequest, ActionLease) {
	t.Helper()
	control, input, _ := agentOutcomeFixture(t)
	original, _, err := control.GetActionLease(input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	now := original.LeaseUntil + 1
	options := OpenOptions{Path: control.path, Owner: "late-outcome-successor", Now: func() int64 { return now }, AuthorizeCutover: true,
		PromotionSignerPublicKey: control.promotionSignerKey, AgentOutcomeSignerPublicKey: control.agentOutcomeSignerKey,
		FleetID: control.fleetID, Environment: control.environment}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = successor.Close() })
	claim, err := successor.AdmitAndClaimAction(AdmissionRequest{ActionID: input.ActionID, Owner: "successor-worker", LeaseSeconds: 60})
	if err != nil || claim.Record.State != "RECONCILING" || claim.Lease.Epoch != original.Epoch+1 || claim.Lease.WriterFencingToken <= original.WriterFencingToken {
		t.Fatalf("fixture did not exercise a real action and writer takeover: %+v %v", claim, err)
	}
	input.Epoch = claim.Lease.Epoch
	return successor, input, claim.Lease
}

func TestAgentNodeOutcomeTakeoverRetainsOriginalProofWithoutFreshDispatch(t *testing.T) {
	control, input, lease := agentLateOutcomeFixture(t)
	before, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	locks, err := control.GetResourceLeases(input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	runBefore, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	stale := input
	stale.Epoch = input.Receipt.Evidence.Fence.ExecutionEpoch
	if _, err := control.RecordAgentNodeOutcome(stale); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("old request fence retained mutation authority: %v", err)
	}
	first, err := control.RecordAgentNodeOutcome(input)
	if err != nil || first.AlreadyRecorded {
		t.Fatalf("current writer lost the independently signed historical effect: %v", err)
	}
	if replay, err := control.RecordAgentNodeOutcome(input); err != nil || !replay.AlreadyRecorded || replay.ReceiptSHA256 != first.ReceiptSHA256 {
		t.Fatalf("historical receipt replay changed proof: %v", err)
	}
	after, _, err := control.Get("action", input.ActionID)
	if err != nil || after.State != "RECONCILING" || after.ExecutionEpoch != input.Epoch || after.Revision != before.Revision+1 {
		t.Fatalf("proof ingestion changed action settlement: %+v %v", after, err)
	}
	_, intent, receipt, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || intent.ExecutionEpoch == input.Epoch || !proto.Equal(receipt, input.Receipt) {
		t.Fatalf("successor fabricated or lost original evidence: %v", err)
	}
	currentLocks, err := control.GetResourceLeases(input.ActionID)
	runAfter, _, runErr := control.AgentRunMetadata(input.RunID)
	if err != nil || runErr != nil || !reflect.DeepEqual(locks, currentLocks) || !reflect.DeepEqual(runBefore, runAfter) {
		t.Fatal("historical proof altered resources or public phase")
	}
	fresh, err := control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{OperationID: "late-outcome-redispatch", Actor: input.Actor,
		ActionID: input.ActionID, Epoch: input.Epoch, RunID: input.RunID, NodeID: input.NodeID, Owner: lease.Owner, ClaimToken: lease.ClaimToken})
	if err == nil || fresh.Fresh {
		t.Fatal("historical outcome authorized another provider dispatch")
	}
}

func TestAgentNodeOutcomeTakeoverRefusesMissingAmbiguousOrCorruptAdmission(t *testing.T) {
	for _, change := range []string{"missing", "duplicate", "writer", "revision", "time", "manifest", "current_resource", "forged_signature"} {
		t.Run(change, func(t *testing.T) {
			control, input, _ := agentLateOutcomeFixture(t)
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			count := countControlEvents(t, control)
			var statement string
			switch change {
			case "missing":
				statement = "DELETE FROM action_lease_events WHERE epoch=1"
			case "duplicate":
				statement = `CREATE TEMP VIEW action_lease_events AS SELECT * FROM main.action_lease_events
					UNION ALL SELECT * FROM main.action_lease_events WHERE epoch=1`
			case "writer":
				statement = "UPDATE action_lease_events SET writer_fencing_token=writer_fencing_token+1 WHERE epoch=1"
			case "revision":
				statement = "UPDATE action_lease_events SET claim_revision=claim_revision+1 WHERE epoch=1"
			case "time":
				statement = "UPDATE action_lease_events SET recorded_at=recorded_at+1 WHERE epoch=1"
			case "manifest":
				statement = "UPDATE action_lease_events SET resource_keys_json='[]' WHERE epoch=1"
			case "current_resource":
				statement = "DELETE FROM action_resource_leases"
			case "forged_signature":
				input.Receipt.Signature[0] ^= 1
			}
			if statement != "" {
				if _, err := control.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.RecordAgentNodeOutcome(input); err == nil {
				t.Fatal("unproven historical admission gained durable authority")
			}
			after, _, err := control.Get("action", input.ActionID)
			if err != nil || after.Revision != before.Revision || !bytes.Equal(after.Payload, before.Payload) || countControlEvents(t, control) != count {
				t.Fatalf("historical proof rejection retained partial action history: %v", err)
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatalf("rejected historical proof retained an applied replay receipt: %v", err)
			}
		})
	}
}
