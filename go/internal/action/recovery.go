package action

import (
	"context"
	"encoding/json"
	"errors"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

const (
	recoveryApplied          = "APPLIED"
	recoveryRecordedNoEffect = "FAILED_BEFORE_EFFECT"
	recoveryUnknown          = "EFFECT_UNKNOWN"
	recoveryStillReconciling = "RECONCILING"
)

// ReconcileClaimedStorageAction inspects the original provider effect under a
// live native claim. It never executes a write, never rebinds dispatch, and
// never treats the current claim epoch as the original storage identity.
// This remains a non-authoritative qualification path: its APPLIED settlement
// must migrate to the baseline verification lifecycle before production wiring.
func (c *Coordinator) ReconcileClaimedStorageAction(ctx context.Context, claim store.ActionLease, assertedOperationID string) (*actionv1.StorageMutationResponse, error) {
	if c == nil || c.store == nil || c.worker == nil || ctx == nil {
		return nil, internalprotocol.ErrUnknownEffect
	}
	if c.authoritative {
		return nil, store.ErrCutoverNotAuthorized
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	owner, ok := c.store.(leasedControlStore)
	if !ok {
		return nil, store.ErrActionLeaseRequired
	}
	if claim.Owner == "" || claim.WriterFencingToken != owner.Writer().FencingToken {
		return nil, store.ErrActionLeaseStale
	}
	record, exists, err := owner.Get("action", claim.ActionID)
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, ErrActionNotFound
	}
	if record.ExecutionEpoch != claim.Epoch {
		return nil, ErrActionExecutionStale
	}
	if record.State != "RECONCILING" && record.State != "EFFECT_UNKNOWN" {
		return nil, internalprotocol.ErrUnknownEffect
	}
	dispatch, bound, err := owner.GetLeasedStorageDispatch(claim.ActionID, claim.Epoch, claim.ClaimToken)
	if err != nil {
		return nil, err
	}
	if !bound || dispatch.Intent.ActionID != claim.ActionID || dispatch.Intent.OperationID == "" {
		return nil, ErrStorageDispatchUnbound
	}
	if assertedOperationID != "" && assertedOperationID != dispatch.Intent.OperationID {
		return nil, ErrStorageDispatchUnbound
	}
	renewal := store.ActionLeaseRenewal{ActionID: claim.ActionID, Epoch: claim.Epoch,
		Owner: claim.Owner, ClaimToken: claim.ClaimToken, LeaseSeconds: 60}
	activeClaim, err := owner.RenewActionLease(renewal)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	fence := &commonv1.ActionFence{ActionId: dispatch.Intent.ActionID, ExecutionEpoch: dispatch.Intent.ExecutionEpoch}
	resp, queryErr, leaseErr := c.callWithActionLease(ctx, owner, activeClaim, renewal, func(rpcCtx context.Context) (*actionv1.StorageMutationResponse, error) {
		return c.worker.QueryStorageEffect(rpcCtx, proto.Clone(fence).(*commonv1.ActionFence), dispatch.Intent.OperationID, c.bearerToken)
	})
	if leaseErr != nil {
		_, markErr := owner.MarkActionEffectUnknown(claim.ActionID, claim.Epoch, claim.ClaimToken)
		return resp, errors.Join(ErrStorageMutationUncertain, queryErr, leaseErr, markErr)
	}
	now := c.now()
	lease := owner.Writer()
	if now >= lease.LeaseUntil || lease.FencingToken != claim.WriterFencingToken {
		return nil, ErrWriterLeaseLost
	}
	current, exists, err := owner.Get("action", claim.ActionID)
	if err != nil {
		return nil, err
	}
	if !exists || current.ExecutionEpoch != claim.Epoch {
		return nil, ErrActionExecutionStale
	}
	switch classifyRecoveredStorageEffect(resp, queryErr, fence, dispatch.Intent.OperationID) {
	case recoveryApplied:
		payload, _ := json.Marshal(map[string]any{
			"etag": resp.Etag, "effectId": resp.EffectId, "providerMetadata": resp.ProviderMetadata,
			"operationId": dispatch.Intent.OperationID, "dispatchEpoch": dispatch.Intent.ExecutionEpoch, "reconciled": true,
		})
		if _, err := owner.CompleteAction(claim.ActionID, claim.Epoch, claim.ClaimToken, payload); err != nil {
			return resp, err
		}
		return resp, nil
	case recoveryRecordedNoEffect:
		payload, _ := json.Marshal(map[string]any{"operationId": dispatch.Intent.OperationID, "dispatchEpoch": dispatch.Intent.ExecutionEpoch, "reconciled": true})
		if _, err := owner.FailAction(claim.ActionID, claim.Epoch, claim.ClaimToken, payload); err != nil {
			return resp, err
		}
		return resp, nil
	case recoveryStillReconciling:
		return resp, ErrStorageMutationUncertain
	default:
		_, markErr := owner.MarkActionEffectUnknown(claim.ActionID, claim.Epoch, claim.ClaimToken)
		return resp, errors.Join(ErrStorageMutationUncertain, queryErr, markErr)
	}
}

func classifyRecoveredStorageEffect(resp *actionv1.StorageMutationResponse, queryErr error, fence *commonv1.ActionFence, operationID string) string {
	if resp == nil || fence == nil || operationID == "" || !matchesStorageResult(resp, fence, operationID) {
		return recoveryUnknown
	}
	switch resp.Status {
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED:
		if queryErr == nil && resp.State == commonv1.EffectState_EFFECT_STATE_APPLIED {
			return recoveryApplied
		}
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_RECONCILING:
		return recoveryStillReconciling
	case actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED:
		code := ""
		if resp.Error != nil {
			code = resp.Error.Code
		}
		if resp.State == commonv1.EffectState_EFFECT_STATE_NOT_APPLIED && isRecordedNoEffect(resp.Status, code) &&
			(queryErr == nil || errors.Is(queryErr, internalprotocol.ErrStoragePreconditionRejected)) {
			return recoveryRecordedNoEffect
		}
	}
	return recoveryUnknown
}
