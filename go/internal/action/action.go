package action

import (
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func BindFence(actionID string, epoch uint64) error {
	return internalprotocol.ValidateFence(&internalprotocol.ActionFence{ActionId: actionID, ExecutionEpoch: epoch})
}

func ExecuteBackup(_ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func ExecuteRepair(_ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func ExecuteRestore(_ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func ExecuteRebalance(_ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func ExecuteFederatedTransfer(_ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func SignReadiness(_ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func Dispatch(kind internalprotocol.CommandKind, fence *internalprotocol.ActionFence, liveEpoch uint64) error {
	return internalprotocol.PlanNative(kind, fence, liveEpoch)
}

func VerifyProof(fence *internalprotocol.ActionFence, liveEpoch uint64, receiptDigest, commitDigest string) error {
	if err := internalprotocol.AdmitCommand(fence, liveEpoch); err != nil {
		return err
	}
	_, _ = receiptDigest, commitDigest
	return internalprotocol.ErrProofNotAuthoritative
}

func ShadowOnly(snapshot map[string]any) bool {
	return protocol.AsString(snapshot["mode"]) != "authoritative"
}
