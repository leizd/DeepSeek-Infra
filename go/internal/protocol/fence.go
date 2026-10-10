package protocol

import (
	"errors"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
)

var (
	ErrEmptyActionID                    = errors.New("EMPTY_ACTION_ID")
	ErrZeroEpoch                        = errors.New("ZERO_EXECUTION_EPOCH")
	ErrStaleEpoch                       = errors.New("STALE_EXECUTION_EPOCH")
	ErrFenceMismatch                    = errors.New("FENCE_MISMATCH")
	ErrUnknownEffect                    = errors.New("EFFECT_UNKNOWN")
	ErrMutationDenied                   = errors.New("MUTATION_DENIED")
	ErrStorageNotAuthoritative          = errors.New("STORAGE_NOT_AUTHORITATIVE")
	ErrTransferNotAuthoritative         = errors.New("TRANSFER_NOT_AUTHORITATIVE")
	ErrFederationNotAuthoritative       = errors.New("FEDERATION_NOT_AUTHORITATIVE")
	ErrProofNotAuthoritative            = errors.New("PROOF_NOT_AUTHORITATIVE")
	ErrServiceAuthenticationUnavailable = errors.New("SERVICE_AUTHENTICATION_UNAVAILABLE")
	ErrAuthenticationMissing            = errors.New("AUTHENTICATION_MISSING")
	ErrAuthenticationInvalid            = errors.New("AUTHENTICATION_INVALID")
	ErrStoragePreconditionRejected      = errors.New("PRECONDITION_REJECTED")
	ErrStorageReplayRejected            = errors.New("REPLAY_REJECTED")
	ErrStorageUnknownEffectRetryBlocked = errors.New("UNKNOWN_EFFECT_RETRY_BLOCKED")
	ErrStorageTargetMismatch            = errors.New("TARGET_MISMATCH")
	ErrStorageDigestMismatch            = errors.New("DIGEST_MISMATCH")
	ErrStorageWorkerWithoutAuthority    = errors.New("WORKER_WITHOUT_AUTHORITY")
	ErrStorageOperationInvalid          = errors.New("OPERATION_INVALID")
	ErrStorageTransportUnavailable      = errors.New("STORAGE_TRANSPORT_UNAVAILABLE")
	ErrStorageTransportError            = errors.New("STORAGE_TRANSPORT_ERROR")
)

type CommandKind = actionv1.CommandKind

const (
	CommandUnspecified              = actionv1.CommandKind_COMMAND_KIND_UNSPECIFIED
	CommandExecuteBackup            = actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP
	CommandExecuteRestore           = actionv1.CommandKind_COMMAND_KIND_EXECUTE_RESTORE
	CommandExecuteRepair            = actionv1.CommandKind_COMMAND_KIND_EXECUTE_REPAIR
	CommandExecuteRebalance         = actionv1.CommandKind_COMMAND_KIND_EXECUTE_REBALANCE
	CommandExecuteFederatedTransfer = actionv1.CommandKind_COMMAND_KIND_EXECUTE_FEDERATED_TRANSFER
	CommandSignReadiness            = actionv1.CommandKind_COMMAND_KIND_SIGN_READINESS
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

func PlanNative(kind CommandKind, fence *ActionFence, liveEpoch uint64) error {
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

type EffectState = commonv1.EffectState

const (
	EffectUnspecified = commonv1.EffectState_EFFECT_STATE_UNSPECIFIED
	EffectNotApplied  = commonv1.EffectState_EFFECT_STATE_NOT_APPLIED
	EffectApplied     = commonv1.EffectState_EFFECT_STATE_APPLIED
	EffectUnknown     = commonv1.EffectState_EFFECT_STATE_UNKNOWN
)

type ActionFence = commonv1.ActionFence

func ValidateFence(fence *ActionFence) error {
	if fence == nil || fence.ActionId == "" {
		return ErrEmptyActionID
	}
	if fence.ExecutionEpoch == 0 {
		return ErrZeroEpoch
	}
	return nil
}

func AdmitCommand(fence *ActionFence, liveEpoch uint64) error {
	if err := ValidateFence(fence); err != nil {
		return err
	}
	if fence.ExecutionEpoch < liveEpoch {
		return ErrStaleEpoch
	}
	if liveEpoch == 0 || fence.ExecutionEpoch != liveEpoch {
		return ErrFenceMismatch
	}
	return nil
}

// ValidateAuthoritativeEpochUpdate is reserved for the control-plane owner that
// establishes or advances the live epoch. Effect admission must use AdmitCommand.
func ValidateAuthoritativeEpochUpdate(fence *ActionFence, liveEpoch uint64) error {
	if err := ValidateFence(fence); err != nil {
		return err
	}
	if liveEpoch != 0 && fence.ExecutionEpoch < liveEpoch {
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
