package action

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

type leasedControlStore interface {
	ControlStore
	RenewActionLease(store.ActionLeaseRenewal) (store.ActionLease, error)
	ClaimLeasedStorageDispatch(store.Record, store.StorageDispatchIntent, string) error
	CompleteAction(string, uint64, string, json.RawMessage) (store.Record, error)
	FailAction(string, uint64, string, json.RawMessage) (store.Record, error)
	MarkActionEffectUnknown(string, uint64, string) (store.Record, error)
}

// ExecuteClaimedStorageAction consumes an explicit native admission claim. It
// never falls back to an unbound dispatch or infers credentials from the database.
// Production remains disabled until auth, signed effects and cutover qualify.
func (c *Coordinator) ExecuteClaimedStorageAction(ctx context.Context, claim store.ActionLease, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
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
	if record.State != "CLAIMED" {
		return nil, ErrStorageMutationUncertain
	}
	if req == nil {
		return nil, store.ErrInvalidStorageIntent
	}
	if req.Fence != nil && (req.Fence.ActionId != claim.ActionID || req.Fence.ExecutionEpoch != claim.Epoch) {
		return nil, internalprotocol.ErrFenceMismatch
	}
	intent, err := storageDispatchIntent(req, record)
	if err != nil {
		return nil, err
	}
	req = proto.Clone(req).(*actionv1.StorageMutationRequest)
	fence := &commonv1.ActionFence{ActionId: claim.ActionID, ExecutionEpoch: claim.Epoch}
	req.Fence = proto.Clone(fence).(*commonv1.ActionFence)
	renewal := store.ActionLeaseRenewal{ActionID: claim.ActionID, Epoch: claim.Epoch,
		Owner: claim.Owner, ClaimToken: claim.ClaimToken, LeaseSeconds: 60}
	activeClaim, err := owner.RenewActionLease(renewal)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	record.Revision++
	record.State = "EXECUTING"
	if err := owner.ClaimLeasedStorageDispatch(record, intent, claim.ClaimToken); err != nil {
		return nil, err
	}
	if cause := ctx.Err(); cause != nil {
		_, markErr := owner.MarkActionEffectUnknown(claim.ActionID, claim.Epoch, claim.ClaimToken)
		return nil, errors.Join(ErrStorageMutationUncertain, cause, markErr)
	}
	leaseNow := time.Now().Unix()
	if c.now != nil {
		leaseNow = c.now()
	}
	leaseLimit := min(activeClaim.LeaseUntil, owner.Writer().LeaseUntil)
	if leaseNow < activeClaim.UpdatedAt || leaseNow >= leaseLimit {
		_, markErr := owner.MarkActionEffectUnknown(claim.ActionID, claim.Epoch, claim.ClaimToken)
		return nil, errors.Join(ErrStorageMutationUncertain, store.ErrActionLeaseExpired, markErr)
	}

	rpcCtx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	interval := c.leaseHeartbeatInterval
	if interval <= 0 || interval >= 60*time.Second {
		interval = 20 * time.Second
	}
	// Writer lifetimes are configurable and may be shorter than the action TTL.
	// Bound before converting to Duration, and leave room for renewal latency.
	interval = min(interval, time.Duration(min(leaseLimit-leaseNow, 60))*time.Second/3)
	stop, stopped := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(stopped)
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-stop:
				return
			case <-rpcCtx.Done():
				return
			case <-ticker.C:
				if _, err := owner.RenewActionLease(renewal); err != nil {
					cancel(err)
					return
				}
			}
		}
	}()
	resp, dispatchErr := c.worker.ExecuteStorageMutation(rpcCtx, req, c.bearerToken)
	close(stop)
	<-stopped // No heartbeat can race terminal settlement or outlive this call.
	uncertain := func(cause error) (*actionv1.StorageMutationResponse, error) {
		_, markErr := owner.MarkActionEffectUnknown(claim.ActionID, claim.Epoch, claim.ClaimToken)
		return resp, errors.Join(ErrStorageMutationUncertain, cause, markErr)
	}
	if cause := context.Cause(rpcCtx); cause != nil {
		return uncertain(cause)
	}
	if _, err := owner.RenewActionLease(renewal); err != nil {
		return uncertain(err)
	}
	if resp == nil || !matchesStorageResult(resp, fence, intent.OperationID) {
		return uncertain(dispatchErr)
	}
	if dispatchErr == nil && resp.Status == actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED && resp.State == commonv1.EffectState_EFFECT_STATE_APPLIED {
		payload, _ := json.Marshal(map[string]any{"etag": resp.Etag, "effectId": resp.EffectId,
			"providerMetadata": resp.ProviderMetadata, "operationId": intent.OperationID})
		if _, err := owner.CompleteAction(claim.ActionID, claim.Epoch, claim.ClaimToken, payload); err != nil {
			return uncertain(err)
		}
		return resp, nil
	}
	code := ""
	if resp.Error != nil {
		code = resp.Error.Code
	}
	if resp.State == commonv1.EffectState_EFFECT_STATE_NOT_APPLIED && isRecordedNoEffect(resp.Status, code) &&
		(dispatchErr == nil || errors.Is(dispatchErr, internalprotocol.ErrStoragePreconditionRejected)) {
		payload, _ := json.Marshal(map[string]any{"operationId": intent.OperationID, "error": code})
		if _, err := owner.FailAction(claim.ActionID, claim.Epoch, claim.ClaimToken, payload); err != nil {
			return uncertain(err)
		}
		return resp, dispatchErr
	}
	return uncertain(dispatchErr)
}
