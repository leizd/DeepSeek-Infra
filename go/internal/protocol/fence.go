package protocol

import "errors"

var (
	ErrEmptyActionID              = errors.New("EMPTY_ACTION_ID")
	ErrZeroEpoch                  = errors.New("ZERO_EXECUTION_EPOCH")
	ErrStaleEpoch                 = errors.New("STALE_EXECUTION_EPOCH")
	ErrFenceMismatch              = errors.New("FENCE_MISMATCH")
	ErrUnknownEffect              = errors.New("EFFECT_UNKNOWN")
	ErrMutationDenied             = errors.New("MUTATION_DENIED")
	ErrStorageNotAuthoritative    = errors.New("STORAGE_NOT_AUTHORITATIVE")
	ErrTransferNotAuthoritative   = errors.New("TRANSFER_NOT_AUTHORITATIVE")
	ErrFederationNotAuthoritative = errors.New("FEDERATION_NOT_AUTHORITATIVE")
	ErrProofNotAuthoritative      = errors.New("PROOF_NOT_AUTHORITATIVE")
)

type CommandKind int

const (
	CommandUnspecified CommandKind = iota
	CommandExecuteBackup
	CommandExecuteRestore
	CommandExecuteRepair
	CommandExecuteRebalance
	CommandExecuteFederatedTransfer
	CommandSignReadiness
)

func IsStorageCommand(kind CommandKind) bool {
	return kind == CommandExecuteBackup || kind == CommandExecuteRestore || kind == CommandExecuteRepair || kind == CommandExecuteRebalance
}

func IsTransferCommand(kind CommandKind) bool {
	return kind == CommandExecuteFederatedTransfer
}

func IsFederationCommand(kind CommandKind) bool {
	return kind == CommandSignReadiness
}

func KindFromName(name string) (CommandKind, bool) {
	switch name {
	case "ExecuteBackup":
		return CommandExecuteBackup, true
	case "ExecuteRestore":
		return CommandExecuteRestore, true
	case "ExecuteRepair":
		return CommandExecuteRepair, true
	case "ExecuteRebalance":
		return CommandExecuteRebalance, true
	case "ExecuteFederatedTransfer":
		return CommandExecuteFederatedTransfer, true
	case "SignReadiness":
		return CommandSignReadiness, true
	default:
		return CommandUnspecified, false
	}
}

func PlanNative(kind CommandKind, fence ActionFence, liveEpoch uint64) error {
	if err := AdmitCommand(fence, liveEpoch); err != nil {
		return err
	}
	switch {
	case IsTransferCommand(kind):
		return ErrTransferNotAuthoritative
	case IsFederationCommand(kind):
		return ErrFederationNotAuthoritative
	case IsStorageCommand(kind):
		return ErrStorageNotAuthoritative
	default:
		return ErrUnknownEffect
	}
}

type EffectState int

const (
	EffectUnspecified EffectState = iota
	EffectNotApplied
	EffectApplied
	EffectUnknown
)

type ActionFence struct {
	ActionID       string
	ExecutionEpoch uint64
}

func ValidateFence(fence ActionFence) error {
	if fence.ActionID == "" {
		return ErrEmptyActionID
	}
	if fence.ExecutionEpoch == 0 {
		return ErrZeroEpoch
	}
	return nil
}

func AdmitCommand(fence ActionFence, liveEpoch uint64) error {
	if err := ValidateFence(fence); err != nil {
		return err
	}
	if fence.ExecutionEpoch < liveEpoch {
		return ErrStaleEpoch
	}
	return nil
}

func InterpretRemoteOutcome(state EffectState) (EffectState, error) {
	switch state {
	case EffectApplied, EffectNotApplied:
		return state, nil
	default:
		return EffectUnknown, ErrUnknownEffect
	}
}

func DenyMutation() error {
	return ErrMutationDenied
}
