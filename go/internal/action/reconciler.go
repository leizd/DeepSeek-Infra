package action

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

var (
	ErrActionNotFound           = errors.New("ACTION_NOT_FOUND")
	ErrActionExecutionStale     = errors.New("ACTION_EXECUTION_STALE")
	ErrWriterLeaseLost          = errors.New("WRITER_LEASE_LOST")
	ErrStorageMutationUncertain = errors.New("STORAGE_MUTATION_UNCERTAIN")
	ErrStorageDispatchUnbound   = errors.New("STORAGE_DISPATCH_UNBOUND")
)

type ControlStore interface {
	Get(domain, id string) (store.Record, bool, error)
	Put(record store.Record) error
	ClaimStorageDispatch(record store.Record, intent store.StorageDispatchIntent) error
	GetStorageDispatch(actionID string, epoch uint64) (store.StorageDispatch, bool, error)
	Writer() store.WriterLease
}

type WorkerClient interface {
	ExecuteStorageMutation(ctx context.Context, request *actionv1.StorageMutationRequest, bearerToken string) (*actionv1.StorageMutationResponse, error)
	QueryStorageEffect(ctx context.Context, fence *commonv1.ActionFence, operationID string, bearerToken string) (*actionv1.StorageMutationResponse, error)
}

type Coordinator struct {
	store                  ControlStore
	worker                 WorkerClient
	bearerToken            string
	now                    func() int64
	authoritative          bool
	leaseHeartbeatInterval time.Duration
}

type CoordinatorOption func(*Coordinator)

func WithNow(now func() int64) CoordinatorOption {
	return func(c *Coordinator) {
		c.now = now
	}
}

func WithBearerToken(token string) CoordinatorOption {
	return func(c *Coordinator) {
		c.bearerToken = token
	}
}

func WithAuthoritative(authoritative bool) CoordinatorOption {
	return func(c *Coordinator) {
		c.authoritative = authoritative
	}
}

func NewCoordinator(store ControlStore, worker WorkerClient, opts ...CoordinatorOption) *Coordinator {
	c := &Coordinator{
		store:                  store,
		worker:                 worker,
		leaseHeartbeatInterval: 20 * time.Second,
		now: func() int64 {
			return time.Now().Unix()
		},
	}
	for _, opt := range opts {
		opt(c)
	}
	return c
}

func (c *Coordinator) ExecuteStorageAction(ctx context.Context, actionID string, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
	if c == nil || c.store == nil || c.worker == nil {
		return nil, internalprotocol.ErrUnknownEffect
	}
	if req == nil {
		return nil, internalprotocol.ErrEmptyActionID
	}

	// 1. Cutover authorization check: production authoritative without cutover approval fails closed.
	if c.authoritative {
		return nil, store.ErrCutoverNotAuthorized
	}

	// 2. Fetch action from durable store
	record, exists, err := c.store.Get("action", actionID)
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, ErrActionNotFound
	}

	// 3. Verify writer lease is held
	lease := c.store.Writer()
	now := c.now()
	if now >= lease.LeaseUntil || lease.FencingToken < 1 {
		return nil, ErrWriterLeaseLost
	}

	// 4. Validate fence match
	if req.Fence != nil && (req.Fence.ActionId != record.ID || req.Fence.ExecutionEpoch != record.ExecutionEpoch) {
		return nil, internalprotocol.ErrFenceMismatch
	}

	// EXECUTING is a durable dispatch claim, including after a crash. Only the
	// caller that commits CLAIMED -> EXECUTING below may dispatch; retries reconcile.
	if record.State == "EFFECT_UNKNOWN" || record.State == "EXECUTING" {
		return nil, ErrStorageMutationUncertain
	}
	if record.State != "PENDING" && record.State != "CLAIMED" {
		return nil, internalprotocol.ErrUnknownEffect
	}
	intent, err := storageDispatchIntent(req, record)
	if err != nil {
		return nil, err
	}
	// Snapshot the bounded qualification request without changing caller-owned
	// protobufs. Result validation uses immutable metadata, not this RPC argument.
	req = proto.Clone(req).(*actionv1.StorageMutationRequest)
	fence := &commonv1.ActionFence{ActionId: intent.ActionID, ExecutionEpoch: intent.ExecutionEpoch}
	req.Fence = proto.Clone(fence).(*commonv1.ActionFence)

	// 6. Claim action: PENDING -> CLAIMED
	if record.State == "PENDING" {
		record.Revision++
		record.State = "CLAIMED"
		if err := c.store.Put(record); err != nil {
			return nil, err
		}
	}

	// 7. Transition CLAIMED -> EXECUTING
	if record.State == "CLAIMED" {
		record.Revision++
		record.State = "EXECUTING"
		if err := c.store.ClaimStorageDispatch(record, intent); err != nil {
			return nil, err
		}
	}

	// 8. Dispatch to Rust Worker
	resp, dispatchErr := c.worker.ExecuteStorageMutation(ctx, req, c.bearerToken)

	// 9. Re-check writer lease after dispatch!
	postLease := c.store.Writer()
	postNow := c.now()
	if postNow >= postLease.LeaseUntil || postLease.FencingToken != lease.FencingToken {
		// Lease was lost during dispatch. Remote write might have happened or not.
		// Move to EFFECT_UNKNOWN so successor or later reconciliation can resolve it.
		record.Revision++
		record.State = "EFFECT_UNKNOWN"
		_ = c.store.Put(record)
		return nil, ErrWriterLeaseLost
	}

	// 10. Check epoch has not been superseded
	current, exists, getErr := c.store.Get("action", actionID)
	if getErr != nil || !exists || current.ExecutionEpoch != record.ExecutionEpoch {
		record.Revision++
		record.State = "EFFECT_UNKNOWN"
		_ = c.store.Put(record)
		return nil, ErrActionExecutionStale
	}

	// 11. Handle response
	if dispatchErr == nil && resp != nil && resp.Status == actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED &&
		resp.State == commonv1.EffectState_EFFECT_STATE_APPLIED && matchesStorageResult(resp, fence, intent.OperationID) {
		payloadMap := map[string]any{
			"etag":             resp.Etag,
			"effectId":         resp.EffectId,
			"providerMetadata": resp.ProviderMetadata,
			"operationId":      resp.OperationId,
		}
		payloadBytes, _ := json.Marshal(payloadMap)
		record.Revision++
		record.State = "SUCCEEDED"
		record.Payload = payloadBytes
		if err := c.store.Put(record); err != nil {
			return resp, err
		}
		return resp, nil
	}

	// If rejected before effect:
	if isDefiniteFailureBeforeEffect(dispatchErr) {
		payloadMap := map[string]any{
			"error":       dispatchErr.Error(),
			"operationId": intent.OperationID,
		}
		payloadBytes, _ := json.Marshal(payloadMap)
		record.Revision++
		record.State = "FAILED_BEFORE_EFFECT"
		record.Payload = payloadBytes
		if err := c.store.Put(record); err != nil {
			return resp, err
		}
		return resp, dispatchErr
	}

	// Otherwise, outcome is uncertain (lost ACK, timeout, transport error, or effect unknown).
	// Retain uncertain effect: transition to EFFECT_UNKNOWN.
	record.Revision++
	record.State = "EFFECT_UNKNOWN"
	if err := c.store.Put(record); err != nil {
		return resp, err
	}
	if dispatchErr != nil {
		return resp, fmt.Errorf("%w: %v", ErrStorageMutationUncertain, dispatchErr)
	}
	return resp, ErrStorageMutationUncertain
}

func (c *Coordinator) ReconcileStorageAction(ctx context.Context, actionID string, operationID string) (*actionv1.StorageMutationResponse, error) {
	if c == nil || c.store == nil || c.worker == nil {
		return nil, internalprotocol.ErrUnknownEffect
	}
	if c.authoritative {
		return nil, store.ErrCutoverNotAuthorized
	}

	// 1. Fetch action from durable store
	record, exists, err := c.store.Get("action", actionID)
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, ErrActionNotFound
	}

	// If already in terminal state, nothing to reconcile
	if record.State == "SUCCEEDED" || record.State == "FAILED_BEFORE_EFFECT" {
		return nil, nil
	}

	// 2. Check writer lease
	lease := c.store.Writer()
	now := c.now()
	if now >= lease.LeaseUntil || lease.FencingToken < 1 {
		return nil, ErrWriterLeaseLost
	}

	// 3. Resolve only an existing dispatch binding before changing state or querying.
	// A supplied ID is an exact assertion, never a source of recovery identity.
	if record.State != "EXECUTING" && record.State != "EFFECT_UNKNOWN" {
		return nil, internalprotocol.ErrUnknownEffect
	}
	dispatch, bound, err := c.store.GetStorageDispatch(record.ID, record.ExecutionEpoch)
	if err != nil {
		return nil, err
	}
	if !bound || dispatch.Intent.ActionID != record.ID || dispatch.Intent.ExecutionEpoch != record.ExecutionEpoch || dispatch.Intent.OperationID == "" ||
		(operationID != "" && operationID != dispatch.Intent.OperationID) {
		return nil, ErrStorageDispatchUnbound
	}
	operationID = dispatch.Intent.OperationID

	if record.State == "EXECUTING" {
		record.Revision++
		record.State = "EFFECT_UNKNOWN"
		if err := c.store.Put(record); err != nil {
			return nil, err
		}
	}

	fence := &commonv1.ActionFence{
		ActionId:       record.ID,
		ExecutionEpoch: record.ExecutionEpoch,
	}

	// 4. Query effect from worker
	resp, queryErr := c.worker.QueryStorageEffect(ctx, proto.Clone(fence).(*commonv1.ActionFence), operationID, c.bearerToken)

	// 5. Re-check writer lease
	postLease := c.store.Writer()
	postNow := c.now()
	if postNow >= postLease.LeaseUntil || postLease.FencingToken != lease.FencingToken {
		return nil, ErrWriterLeaseLost
	}

	// 6. Check epoch still matches
	current, exists, getErr := c.store.Get("action", actionID)
	if getErr != nil || !exists || current.ExecutionEpoch != record.ExecutionEpoch {
		return nil, ErrActionExecutionStale
	}

	if queryErr == nil && resp != nil && resp.Status == actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED &&
		resp.State == commonv1.EffectState_EFFECT_STATE_APPLIED && matchesStorageResult(resp, fence, operationID) {
		payloadMap := map[string]any{
			"etag":             resp.Etag,
			"effectId":         resp.EffectId,
			"providerMetadata": resp.ProviderMetadata,
			"operationId":      resp.OperationId,
			"reconciled":       true,
		}
		payloadBytes, _ := json.Marshal(payloadMap)
		record.Revision++
		record.State = "SUCCEEDED"
		record.Payload = payloadBytes
		if err := c.store.Put(record); err != nil {
			return resp, err
		}
		return resp, nil
	}

	// If definitely rejected or failed
	if resp != nil && (resp.Status == actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED ||
		resp.Status == actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED) {
		errCode := ""
		if resp.Error != nil {
			errCode = resp.Error.Code
		}
		// A failed query says nothing about an earlier PUT. Only an exact, bound
		// NOT_APPLIED effect result can settle the action; unknown codes fail closed.
		if resp.State != commonv1.EffectState_EFFECT_STATE_NOT_APPLIED ||
			!matchesStorageResult(resp, fence, operationID) ||
			!isRecordedNoEffect(resp.Status, errCode) ||
			(queryErr != nil && !errors.Is(queryErr, internalprotocol.ErrStoragePreconditionRejected)) {
			if queryErr != nil {
				return resp, queryErr
			}
			return resp, ErrStorageMutationUncertain
		}
		payloadMap := map[string]any{
			"reconciled":  true,
			"operationId": operationID,
		}
		if errCode != "" {
			payloadMap["error"] = errCode
		}
		payloadBytes, _ := json.Marshal(payloadMap)
		record.Revision++
		record.State = "FAILED_BEFORE_EFFECT"
		record.Payload = payloadBytes
		if err := c.store.Put(record); err != nil {
			return resp, err
		}
		return resp, nil
	}

	// If still uncertain, keep in EFFECT_UNKNOWN
	if queryErr != nil {
		return resp, queryErr
	}
	return resp, ErrStorageMutationUncertain
}

func matchesStorageResult(resp *actionv1.StorageMutationResponse, fence *commonv1.ActionFence, operationID string) bool {
	return resp.Fence != nil && resp.Fence.ActionId == fence.ActionId &&
		resp.Fence.ExecutionEpoch == fence.ExecutionEpoch && operationID != "" && resp.OperationId == operationID
}

func isRecordedNoEffect(status actionv1.StorageMutationStatus, code string) bool {
	return status == actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED && (code == "Rejected" || code == "PRECONDITION_REJECTED") ||
		status == actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED && code == "Failed"
}

func isDefiniteFailureBeforeEffect(err error) bool {
	if err == nil {
		return false
	}
	return errors.Is(err, internalprotocol.ErrStoragePreconditionRejected) ||
		errors.Is(err, internalprotocol.ErrFenceMismatch) ||
		errors.Is(err, internalprotocol.ErrEmptyActionID) ||
		errors.Is(err, internalprotocol.ErrZeroEpoch) ||
		errors.Is(err, internalprotocol.ErrStaleEpoch) ||
		errors.Is(err, internalprotocol.ErrServiceAuthenticationUnavailable) ||
		errors.Is(err, internalprotocol.ErrAuthenticationMissing) ||
		errors.Is(err, internalprotocol.ErrAuthenticationInvalid) ||
		errors.Is(err, internalprotocol.ErrStorageWorkerWithoutAuthority) ||
		errors.Is(err, internalprotocol.ErrStorageTargetMismatch) ||
		errors.Is(err, internalprotocol.ErrStorageDigestMismatch)
}
