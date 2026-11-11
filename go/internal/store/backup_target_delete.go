package store

import "errors"

var ErrBackupTargetNotFound = errors.New("BACKUP_TARGET_NOT_FOUND")

// DeleteBackupTarget removes a registration through the shared fenced metadata
// transaction, retaining the operation, immutable target history and provider data.
func (store *Control) DeleteBackupTarget(operationID, actionID, actor, targetID string) (OperatorMutationResult, error) {
	_, result, err := store.mutateBackupTarget(operationID, actionID, actor, targetID, targetDeleteCommand)
	return result, err
}
