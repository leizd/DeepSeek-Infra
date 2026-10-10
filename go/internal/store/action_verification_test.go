package store

import (
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
)

func TestActionVerificationPhasesKeepInputsReservationsAndOriginalDispatch(t *testing.T) {
	control, claim := executingLeasedAction(t)
	before, _, err := control.Get("action", claim.Lease.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	original, _, err := control.GetStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch)
	if err != nil {
		t.Fatal(err)
	}
	resources, err := control.GetResourceLeases(claim.Lease.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	verifying, err := control.MarkActionVerifying(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, json.RawMessage(`{"operationId":"observed-original"}`))
	if err != nil || verifying.State != "VERIFYING" {
		t.Fatalf("verification did not start: %v", err)
	}
	var previousPayload, currentPayload map[string]json.RawMessage
	if err := json.Unmarshal(before.Payload, &previousPayload); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(verifying.Payload, &currentPayload); err != nil {
		t.Fatal(err)
	}
	if previousPayload["parameters"] == nil || !reflect.DeepEqual(previousPayload["parameters"], currentPayload["parameters"]) {
		t.Fatal("effect observation replaced action scope")
	}
	if _, err := control.CompleteAction(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("verification skipped risk assessment: %v", err)
	}
	if _, err := control.FailAction(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("applied observation relabeled no effect: %v", err)
	}
	if _, err := control.RenewActionLease(renewalFor(claim.Lease)); err != nil {
		t.Fatal(err)
	}
	assessing, err := control.MarkActionAssessingEffect(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, json.RawMessage(`{"fixtureObservation":true}`))
	if err != nil || assessing.State != "ASSESSING_EFFECT" {
		t.Fatalf("assessment did not start: %v", err)
	}
	if err := json.Unmarshal(assessing.Payload, &currentPayload); err != nil {
		t.Fatal(err)
	}
	if string(currentPayload["nativeEffectObservation"]) != `{"operationId":"observed-original"}` || string(currentPayload["nativeOutcomeVerification"]) != `{"fixtureObservation":true}` || !reflect.DeepEqual(previousPayload["parameters"], currentPayload["parameters"]) {
		t.Fatal("phase observation or original scope was lost")
	}
	gotResources, err := control.GetResourceLeases(claim.Lease.ActionID)
	if err != nil || len(gotResources) != len(resources) {
		t.Fatalf("phase transition released resources: %v", err)
	}
	got, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken)
	if err != nil || !bound || got != original {
		t.Fatalf("phase transition rebound dispatch: %v", err)
	}
	// This exercises a store primitive, not provider/risk verification evidence.
	if _, err := control.CompleteAction(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); err != nil {
		t.Fatal(err)
	}
}

func TestActionVerificationPhaseTakeoversRetainRecoveryIdentity(t *testing.T) {
	for _, assessing := range []bool{false, true} {
		t.Run(map[bool]string{false: "verifying", true: "assessing"}[assessing], func(t *testing.T) {
			control, claim := executingLeasedAction(t)
			original, _, err := control.GetStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := control.MarkActionVerifying(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, json.RawMessage(`{}`)); err != nil {
				t.Fatal(err)
			}
			if assessing {
				if _, err := control.MarkActionAssessingEffect(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, json.RawMessage(`{}`)); err != nil {
					t.Fatal(err)
				}
			}
			control.now = func() int64 { return claim.Lease.LeaseUntil + 1 }
			if _, err := control.RenewActionLease(renewalFor(claim.Lease)); !errors.Is(err, ErrActionLeaseExpired) {
				t.Fatalf("expired phase lease revived: %v", err)
			}
			next, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, LeaseSeconds: 10})
			if err != nil || next.Record.State != "RECONCILING" || next.Lease.Epoch != claim.Lease.Epoch+1 {
				t.Fatalf("phase takeover: %v", err)
			}
			got, bound, err := control.GetLeasedStorageDispatch(next.Lease.ActionID, next.Lease.Epoch, next.Lease.ClaimToken)
			if err != nil || !bound || got != original {
				t.Fatalf("phase takeover lost intent: %v", err)
			}
			if _, err := control.MarkActionAssessingEffect(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseStale) {
				t.Fatalf("old phase owner wrote after takeover: %v", err)
			}
		})
	}
}

func TestActionVerificationRejectsInvalidObservationsAtomically(t *testing.T) {
	for _, raw := range []string{`null`, `[]`, `42`, `"text"`, `{`, `{} {}`, `{"apiKey":"do-not-store"}`, `{"oversized":"` + strings.Repeat("x", maximumPayloadBytes) + `"}`} {
		control, claim := executingLeasedAction(t)
		before, err := control.ExportSnapshot()
		if err != nil {
			t.Fatal(err)
		}
		want := ErrInvalidPayload
		if strings.Contains(raw, "apiKey") {
			want = ErrSecretDetected
		}
		if _, err := control.MarkActionVerifying(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, json.RawMessage(raw)); !errors.Is(err, want) {
			t.Fatalf("invalid observation accepted: %v", err)
		}
		after, err := control.ExportSnapshot()
		if err != nil || !reflect.DeepEqual(before, after) {
			t.Fatalf("invalid observation changed history: %v", err)
		}
	}
}

func TestActionVerificationReservesGlobalAndTargetBudgets(t *testing.T) {
	for _, phase := range []string{"VERIFYING", "ASSESSING_EFFECT"} {
		for _, limit := range []string{"global", "target"} {
			t.Run(phase+"/"+limit, func(t *testing.T) {
				control, claim := executingLeasedAction(t)
				// A result field called parameters must not replace admission scope.
				if _, err := control.MarkActionVerifying(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, json.RawMessage(`{"parameters":{"targetId":"attacker-target"}}`)); err != nil {
					t.Fatal(err)
				}
				if phase == "ASSESSING_EFFECT" {
					if _, err := control.MarkActionAssessingEffect(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); err != nil {
						t.Fatal(err)
					}
				}
				if err := control.Put(Record{Domain: "action", ID: "budget-peer", Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: json.RawMessage(`{"parameters":{"targetId":"target-boundary"}}`)}); err != nil {
					t.Fatal(err)
				}
				policy := AdmissionPolicy{MaxConcurrentActions: 10, MaxConcurrentPerTarget: 1}
				if limit == "global" {
					policy.MaxConcurrentActions = 1
					policy.MaxConcurrentPerTarget = 10
				}
				_, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "budget-peer", Policy: policy})
				if !errors.Is(err, ErrBudgetExceeded) {
					t.Fatalf("verification phase bypassed %s budget: %v", limit, err)
				}
			})
		}
	}
}

func TestActionVerificationUncertaintyKeepsResourcesAndGenericWritesDenied(t *testing.T) {
	for _, phase := range []string{"VERIFYING", "ASSESSING_EFFECT"} {
		t.Run(phase, func(t *testing.T) {
			control, claim := executingLeasedAction(t)
			verifying, err := control.MarkActionVerifying(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil)
			if err != nil {
				t.Fatal(err)
			}
			verifying.Revision++
			verifying.State = "ASSESSING_EFFECT"
			if err := control.Put(verifying); !errors.Is(err, ErrActionLeaseRequired) {
				t.Fatalf("generic writer changed phase: %v", err)
			}
			if phase == "ASSESSING_EFFECT" {
				if _, err := control.MarkActionAssessingEffect(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.MarkActionEffectUnknown(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); err != nil {
				t.Fatal(err)
			}
			resources, err := control.GetResourceLeases(claim.Lease.ActionID)
			if err != nil || len(resources) != len(claim.ResourceKeys) {
				t.Fatalf("uncertain phase released locks: %v", err)
			}
			if _, err := control.MarkActionVerifying(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); err != nil {
				t.Fatal(err)
			}
		})
	}
}
