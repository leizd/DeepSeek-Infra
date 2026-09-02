package protocol

import "errors"

var (
	ErrEmptyActionID  = errors.New("EMPTY_ACTION_ID")
	ErrZeroEpoch      = errors.New("ZERO_EXECUTION_EPOCH")
	ErrStaleEpoch     = errors.New("STALE_EXECUTION_EPOCH")
	ErrFenceMismatch  = errors.New("FENCE_MISMATCH")
	ErrUnknownEffect  = errors.New("EFFECT_UNKNOWN")
	ErrMutationDenied = errors.New("MUTATION_DENIED")
)

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
