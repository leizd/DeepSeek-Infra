package store

import (
	"encoding/json"
	"testing"
)

func TestAdmissionFaultInjectionRollbacks(t *testing.T) {
	faultStages := []string{
		"after_budget_check",
		"after_first_resource",
		"mid_resources",
		"after_action_lease_insert",
		"after_control_event_write",
		"before_final_writer_fence",
		"at_commit",
	}

	for _, stage := range faultStages {
		t.Run(stage, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()

			actionID := "fault-act-" + stage
			if err := control.Put(Record{
				Domain: "action", ID: actionID, Revision: 1, ExecutionEpoch: 1, State: "PENDING",
				Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
			}); err != nil {
				t.Fatal(err)
			}

			// Hook injection point
			control.admissionFaultStage = stage

			req := AdmissionRequest{
				ActionID:     actionID,
				Owner:        "worker-fault",
				LeaseSeconds: 60,
				ResourceKeys: []string{"res-1", "res-2", "res-3"},
			}

			_, err := control.AdmitAndClaimAction(req)
			if err == nil {
				t.Fatalf("expected error for fault stage %s, got nil", stage)
			}

			// Verify invariant 1: Zero orphan resource leases
			for _, resKey := range []string{"res-1", "res-2", "res-3"} {
				var count int
				if err := control.db.QueryRow("SELECT COUNT(*) FROM action_resource_leases WHERE resource_key=?", resKey).Scan(&count); err != nil {
					t.Fatal(err)
				}
				if count != 0 {
					t.Fatalf("stage %s left orphan resource lease for %s", stage, resKey)
				}
			}

			// Verify invariant 2: Zero orphan action leases
			var leaseCount int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM action_leases WHERE action_id=?", actionID).Scan(&leaseCount); err != nil {
				t.Fatal(err)
			}
			if leaseCount != 0 {
				t.Fatalf("stage %s left orphan action lease", stage)
			}

			// Verify invariant 3: Zero action lease events
			var eventCount int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM action_lease_events WHERE action_id=?", actionID).Scan(&eventCount); err != nil {
				t.Fatal(err)
			}
			if eventCount != 0 {
				t.Fatalf("stage %s left orphan action lease events", stage)
			}

			// Verify invariant 4: Action record remains in PENDING with revision 1
			rec, exists, err := control.Get("action", actionID)
			if err != nil || !exists {
				t.Fatalf("failed to fetch action: %v", err)
			}
			if rec.State != "PENDING" || rec.Revision != 1 {
				t.Fatalf("stage %s corrupted action record: %+v", stage, rec)
			}

			// Verify invariant 5: No orphan control_events for revision > 1
			var ctrlEventCount int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM control_events WHERE domain='action' AND record_id=? AND revision>1", actionID).Scan(&ctrlEventCount); err != nil {
				t.Fatal(err)
			}
			if ctrlEventCount != 0 {
				t.Fatalf("stage %s left orphan control event", stage)
			}
		})
	}
}
