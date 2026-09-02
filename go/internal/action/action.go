package action

import (
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func BindFence(actionID string, epoch uint64) error {
	return internalprotocol.ValidateFence(internalprotocol.ActionFence{ActionID: actionID, ExecutionEpoch: epoch})
}

func ExecuteBackup(_ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func ExecuteRepair(_ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func ShadowOnly(snapshot map[string]any) bool {
	return protocol.AsString(snapshot["mode"]) != "authoritative"
}
