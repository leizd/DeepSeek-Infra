package protocol

import "testing"

func TestValidateFenceRejectsEmptyAndZero(t *testing.T) {
	if err := ValidateFence(ActionFence{ActionID: "", ExecutionEpoch: 1}); err != ErrEmptyActionID {
		t.Fatalf("empty id: %v", err)
	}
	if err := ValidateFence(ActionFence{ActionID: "act-1", ExecutionEpoch: 0}); err != ErrZeroEpoch {
		t.Fatalf("zero epoch: %v", err)
	}
}

func TestAdmitCommandRejectsStaleEpoch(t *testing.T) {
	fence := ActionFence{ActionID: "act-1", ExecutionEpoch: 3}
	if err := AdmitCommand(fence, 4); err != ErrStaleEpoch {
		t.Fatalf("stale: %v", err)
	}
	if err := AdmitCommand(fence, 3); err != nil {
		t.Fatalf("matching live epoch: %v", err)
	}
}

func TestUnknownEffectIsNotNotApplied(t *testing.T) {
	state, err := InterpretRemoteOutcome(EffectUnknown)
	if err != ErrUnknownEffect || state != EffectUnknown {
		t.Fatalf("unknown: %v %v", state, err)
	}
	state, err = InterpretRemoteOutcome(EffectUnspecified)
	if err != ErrUnknownEffect || state != EffectUnknown {
		t.Fatalf("unspecified: %v %v", state, err)
	}
	if _, err := InterpretRemoteOutcome(EffectNotApplied); err != nil {
		t.Fatalf("not applied should remain explicit: %v", err)
	}
}

func TestMutationDenied(t *testing.T) {
	if err := DenyMutation(); err != ErrMutationDenied {
		t.Fatalf("mutation: %v", err)
	}
}
