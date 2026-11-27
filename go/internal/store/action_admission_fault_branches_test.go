package store

import (
	"encoding/json"
	"testing"
)

// The admission budget queries and lease reads must surface a storage failure
// rather than treating an unreadable count as zero. A swallowed error there
// admits an action the budget was meant to refuse.

func TestAdmissionBudgetQueriesSurfaceStorageFailure(t *testing.T) {
	// A trigger that denies every write would refuse the setup too, so the fault
	// is installed after the action row exists. The count query then fails while
	// the admission still has to refuse rather than treat it as zero.
	control := openControlAt(t, 1000)
	defer control.Close()
	actionID := "budget-fault"
	if err := control.Put(Record{
		Domain: "action", ID: actionID, Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	// Break the budget view so the count query cannot be read.
	if _, err := control.db.Exec("DROP TABLE IF EXISTS action_journal"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: actionID, Owner: "budget-fault-owner", LeaseSeconds: 60,
	}); err == nil {
		t.Fatal("an unreadable budget was reported as an admitted action")
	}
}

func TestAdmissionRefusesAnUnreadablePayload(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	actionID := "payload-fault"
	// Valid JSON with a parameters object of the wrong shape: the schema accepts
	// the bytes, and admission must refuse rather than read empty parameters.
	if err := control.Put(Record{
		Domain: "action", ID: actionID, Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":"not-an-object"}`),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: actionID, Owner: "payload-fault-owner", LeaseSeconds: 60,
	}); err == nil {
		t.Fatal("a malformed parameters object was admitted")
	}
}

func TestAdmissionDefaultsANonPositiveLease(t *testing.T) {
	// A zero or negative lease is not a refusal: admission substitutes the
	// default 60 seconds, which is what a caller that omits the field relies on.
	for name, lease := range map[string]int64{"zero": 0, "negative": -30} {
		t.Run(name, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			actionID := "lease-default-" + name
			if err := control.Put(Record{
				Domain: "action", ID: actionID, Revision: 1, ExecutionEpoch: 1, State: "PENDING",
				Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
			}); err != nil {
				t.Fatal(err)
			}
			result, err := control.AdmitAndClaimAction(AdmissionRequest{
				ActionID: actionID, Owner: "lease-default-owner", LeaseSeconds: lease,
			})
			if err != nil {
				t.Fatalf("a %s lease was refused instead of defaulted: %v", name, err)
			}
			if result.Lease.LeaseUntil != 1060 {
				t.Fatalf("%s lease ended at %d, want now+60", name, result.Lease.LeaseUntil)
			}
		})
	}
}
