package protocol

import (
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
)

var (
	_ func(*commonv1.ActionFence) error                               = ValidateFence
	_ func(actionv1.CommandKind, *commonv1.ActionFence, uint64) error = PlanNative
	_ func(commonv1.EffectState) (commonv1.EffectState, error)        = InterpretRemoteOutcome
)

func TestValidateFenceRejectsEmptyAndZero(t *testing.T) {
	if err := ValidateFence(nil); err != ErrEmptyActionID {
		t.Fatalf("missing fence: %v", err)
	}
	if err := ValidateFence(&ActionFence{ActionId: "", ExecutionEpoch: 1}); err != ErrEmptyActionID {
		t.Fatalf("empty id: %v", err)
	}
	if err := ValidateFence(&ActionFence{ActionId: "act-1", ExecutionEpoch: 0}); err != ErrZeroEpoch {
		t.Fatalf("zero epoch: %v", err)
	}
}

func TestAdmitCommandRejectsInvalidFence(t *testing.T) {
	if err := AdmitCommand(&ActionFence{ActionId: "", ExecutionEpoch: 1}, 1); err != ErrEmptyActionID {
		t.Fatalf("empty: %v", err)
	}
}

func TestAdmitCommandRejectsStaleEpoch(t *testing.T) {
	fence := &ActionFence{ActionId: "act-1", ExecutionEpoch: 3}
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

func TestPlanNativeMatchesRustAuthorityCodes(t *testing.T) {
	fence := &ActionFence{ActionId: "act-1", ExecutionEpoch: 1}
	if err := PlanNative(CommandExecuteBackup, fence, 0); err != ErrStorageNotAuthoritative {
		t.Fatalf("backup: %v", err)
	}
	if err := PlanNative(CommandExecuteFederatedTransfer, fence, 0); err != ErrTransferNotAuthoritative {
		t.Fatalf("transfer: %v", err)
	}
	if err := PlanNative(CommandSignReadiness, fence, 0); err != ErrFederationNotAuthoritative {
		t.Fatalf("sign: %v", err)
	}
	if err := PlanNative(CommandUnspecified, fence, 0); err != ErrUnknownEffect {
		t.Fatalf("unspecified: %v", err)
	}
	if err := PlanNative(CommandExecuteRepair, &ActionFence{ActionId: "act-1", ExecutionEpoch: 1}, 4); err != ErrStaleEpoch {
		t.Fatalf("stale: %v", err)
	}
	if !IsStorageCommand(CommandExecuteRestore) || IsStorageCommand(CommandExecuteFederatedTransfer) {
		t.Fatal("storage classifier")
	}
	for _, name := range []string{"ExecuteBackup", "ExecuteRestore", "ExecuteRepair", "ExecuteRebalance", "ExecuteFederatedTransfer", "SignReadiness"} {
		if _, ok := KindFromName(name); !ok {
			t.Fatalf("kind %s", name)
		}
	}
	if _, ok := KindFromName("Nope"); ok {
		t.Fatal("unknown kind")
	}
	codes := map[error]string{
		ErrStorageNotAuthoritative:    "STORAGE_NOT_AUTHORITATIVE",
		ErrTransferNotAuthoritative:   "TRANSFER_NOT_AUTHORITATIVE",
		ErrFederationNotAuthoritative: "FEDERATION_NOT_AUTHORITATIVE",
		ErrProofNotAuthoritative:      "PROOF_NOT_AUTHORITATIVE",
		ErrUnknownEffect:              "EFFECT_UNKNOWN",
		ErrStaleEpoch:                 "STALE_EXECUTION_EPOCH",
	}
	for err, want := range codes {
		if err.Error() != want {
			t.Fatalf("%v != %s", err, want)
		}
	}
}
