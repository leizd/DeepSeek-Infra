package action

import (
	"context"
	"errors"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

type fakeControlStore struct {
	records    map[string]store.Record
	dispatches map[dispatchKey]store.StorageDispatch
	lease      store.WriterLease
	getErr     error
	putErr     error
}

type dispatchKey struct {
	action string
	epoch  uint64
}

func newFakeStore() *fakeControlStore {
	return &fakeControlStore{
		records:    make(map[string]store.Record),
		dispatches: make(map[dispatchKey]store.StorageDispatch),
		lease: store.WriterLease{
			Runtime:         store.RuntimeGo,
			Mode:            store.ModeShadow,
			OwnerInstanceID: "inst-1",
			FencingToken:    10,
			LeaseUntil:      1000,
		},
	}
}

func (s *fakeControlStore) Get(domain, id string) (store.Record, bool, error) {
	if s.getErr != nil {
		return store.Record{}, false, s.getErr
	}
	r, ok := s.records[domain+":"+id]
	return r, ok, nil
}

func (s *fakeControlStore) Put(record store.Record) error {
	if s.putErr != nil {
		return s.putErr
	}
	s.records[record.Domain+":"+record.ID] = record
	return nil
}

func (s *fakeControlStore) Writer() store.WriterLease {
	return s.lease
}

func (s *fakeControlStore) ClaimStorageDispatch(record store.Record, intent store.StorageDispatchIntent) error {
	if err := s.Put(record); err != nil {
		return err
	}
	s.dispatches[dispatchKey{record.ID, record.ExecutionEpoch}] = store.StorageDispatch{Intent: intent}
	return nil
}

func (s *fakeControlStore) GetStorageDispatch(actionID string, epoch uint64) (store.StorageDispatch, bool, error) {
	dispatch, exists := s.dispatches[dispatchKey{actionID, epoch}]
	return dispatch, exists, nil
}

// Explicit unit fixture only; production must never infer a dispatch from a row.
func (s *fakeControlStore) seedDispatch(actionID, operationID string) {
	s.dispatches[dispatchKey{actionID, 1}] = store.StorageDispatch{Intent: store.StorageDispatchIntent{
		ActionID: actionID, ExecutionEpoch: 1, OperationID: operationID,
	}}
}

type fakeWorkerClient struct {
	executeResp *actionv1.StorageMutationResponse
	executeErr  error
	queryResp   *actionv1.StorageMutationResponse
	queryErr    error
	onExecute   func()
	onQuery     func()
}

func (w *fakeWorkerClient) ExecuteStorageMutation(_ context.Context, _ *actionv1.StorageMutationRequest, _ string) (*actionv1.StorageMutationResponse, error) {
	if w.onExecute != nil {
		w.onExecute()
	}
	return w.executeResp, w.executeErr
}

func (w *fakeWorkerClient) QueryStorageEffect(_ context.Context, _ *commonv1.ActionFence, _ string, _ string) (*actionv1.StorageMutationResponse, error) {
	if w.onQuery != nil {
		w.onQuery()
	}
	return w.queryResp, w.queryErr
}

func TestExecuteActionNormalConfirmed(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       1,
		ExecutionEpoch: 1,
		State:          "PENDING",
	}

	worker := &fakeWorkerClient{
		executeResp: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
			Etag:        "\"etag-123\"",
			Fence:       &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1},
			EffectId:    "act-1:1",
			OperationId: "op-1",
		},
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	req := storageRequest("op-1")

	resp, err := coord.ExecuteStorageAction(context.Background(), "act-1", req)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.Etag != "\"etag-123\"" {
		t.Fatalf("unexpected etag: %s", resp.Etag)
	}

	rec, exists, _ := st.Get("action", "act-1")
	if !exists || rec.State != "SUCCEEDED" {
		t.Fatalf("expected SUCCEEDED state, got: %+v", rec)
	}
	if rec.Revision != 4 { // PENDING -> CLAIMED (2) -> EXECUTING (3) -> SUCCEEDED (4)
		t.Fatalf("expected revision 4, got %d", rec.Revision)
	}
}

func TestExecuteActionAuthoritativeFailsClosed(t *testing.T) {
	st := newFakeStore()
	coord := NewCoordinator(st, &fakeWorkerClient{}, WithAuthoritative(true))

	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", &actionv1.StorageMutationRequest{})
	if !errors.Is(err, store.ErrCutoverNotAuthorized) {
		t.Fatalf("expected ErrCutoverNotAuthorized, got: %v", err)
	}
}

func TestExecuteActionDefiniteFailureBeforeEffect(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       1,
		ExecutionEpoch: 1,
		State:          "PENDING",
	}

	worker := &fakeWorkerClient{
		executeErr: internalprotocol.ErrStoragePreconditionRejected,
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if !errors.Is(err, internalprotocol.ErrStoragePreconditionRejected) {
		t.Fatalf("expected PreconditionRejected, got: %v", err)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "FAILED_BEFORE_EFFECT" {
		t.Fatalf("expected FAILED_BEFORE_EFFECT, got: %s", rec.State)
	}
}

func TestExecuteActionUncertainOutcomeRetainsEffectUnknown(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       1,
		ExecutionEpoch: 1,
		State:          "PENDING",
	}

	worker := &fakeWorkerClient{
		executeErr: errors.New("deadline exceeded / lost ACK"),
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("expected ErrStorageMutationUncertain, got: %v", err)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "EFFECT_UNKNOWN" {
		t.Fatalf("expected EFFECT_UNKNOWN, got: %s", rec.State)
	}

	// Re-dispatching an uncertain action must be blocked
	_, err = coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("expected blocked re-dispatch, got: %v", err)
	}
}

func TestExecuteActionLeaseLostDuringDispatch(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       1,
		ExecutionEpoch: 1,
		State:          "PENDING",
	}

	timeVal := int64(500)
	worker := &fakeWorkerClient{
		executeResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
		},
		onExecute: func() {
			timeVal = 1001
		},
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return timeVal }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if !errors.Is(err, ErrWriterLeaseLost) {
		t.Fatalf("expected ErrWriterLeaseLost, got: %v", err)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "EFFECT_UNKNOWN" {
		t.Fatalf("expected EFFECT_UNKNOWN after lost lease, got: %s", rec.State)
	}
}

func TestExecuteActionTakeoverFencingTokenDuringDispatch(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       1,
		ExecutionEpoch: 1,
		State:          "PENDING",
	}

	worker := &fakeWorkerClient{
		executeResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
		},
		onExecute: func() {
			st.lease.FencingToken = 99
		},
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if !errors.Is(err, ErrWriterLeaseLost) {
		t.Fatalf("expected ErrWriterLeaseLost on takeover, got: %v", err)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "EFFECT_UNKNOWN" {
		t.Fatalf("expected EFFECT_UNKNOWN after takeover, got: %s", rec.State)
	}
}

func TestReconcileActionTakeoverFencingTokenDuringQuery(t *testing.T) {
	st := newFakeStore()
	st.seedDispatch("act-1", "op-1")
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       2,
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}

	worker := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
			Etag:        "\"etag-1\"",
			EffectId:    "act-1:1",
			OperationId: "op-1",
		},
		onQuery: func() {
			st.lease.FencingToken = 99
		},
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	_, err := coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if !errors.Is(err, ErrWriterLeaseLost) {
		t.Fatalf("expected ErrWriterLeaseLost on takeover during query, got: %v", err)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "EFFECT_UNKNOWN" {
		t.Fatalf("expected EFFECT_UNKNOWN to persist, got: %s", rec.State)
	}
}

func TestReconcileStorageActionConfirmed(t *testing.T) {
	st := newFakeStore()
	st.seedDispatch("act-1", "op-1")
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       2,
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}

	worker := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
			Etag:        "\"etag-reconciled\"",
			Fence:       &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1},
			EffectId:    "act-1:1",
			OperationId: "op-1",
		},
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	resp, err := coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resp.Etag != "\"etag-reconciled\"" {
		t.Fatalf("unexpected etag: %s", resp.Etag)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "SUCCEEDED" {
		t.Fatalf("expected SUCCEEDED after reconciliation, got: %s", rec.State)
	}
}

func TestReconcileStorageActionRejected(t *testing.T) {
	st := newFakeStore()
	st.seedDispatch("act-1", "op-1")
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       2,
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}

	worker := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED,
			State:       commonv1.EffectState_EFFECT_STATE_NOT_APPLIED,
			Fence:       &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1},
			OperationId: "op-1",
			Error:       &commonv1.ErrorDetail{Code: "PRECONDITION_REJECTED"},
		},
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	_, err := coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "FAILED_BEFORE_EFFECT" {
		t.Fatalf("expected FAILED_BEFORE_EFFECT after rejection, got: %s", rec.State)
	}
}

func TestReconcileStorageActionStillUncertain(t *testing.T) {
	st := newFakeStore()
	st.seedDispatch("act-1", "op-1")
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       2,
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}

	worker := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN,
			State:  commonv1.EffectState_EFFECT_STATE_UNKNOWN,
		},
		queryErr: internalprotocol.ErrUnknownEffect,
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	_, err := coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("expected ErrUnknownEffect, got: %v", err)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "EFFECT_UNKNOWN" {
		t.Fatalf("expected EFFECT_UNKNOWN to persist, got: %s", rec.State)
	}
}

func TestReconcileTerminalNoop(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       2,
		ExecutionEpoch: 1,
		State:          "SUCCEEDED",
	}

	coord := NewCoordinator(st, &fakeWorkerClient{}, WithNow(func() int64 { return 500 }))
	resp, err := coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if err != nil || resp != nil {
		t.Fatalf("expected nil, nil on terminal state, got %v, %v", resp, err)
	}
}

func TestCoordinatorOptionsAndDefaults(t *testing.T) {
	st := newFakeStore()
	worker := &fakeWorkerClient{}
	coord := NewCoordinator(st, worker, WithBearerToken("secret-token"))
	if coord.bearerToken != "secret-token" {
		t.Fatalf("expected bearer token secret-token, got %s", coord.bearerToken)
	}
	if coord.now() <= 0 {
		t.Fatalf("expected default now func to return positive timestamp, got %d", coord.now())
	}

	var nilCoord *Coordinator
	if _, err := nilCoord.ExecuteStorageAction(context.Background(), "a", &actionv1.StorageMutationRequest{}); !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("expected ErrUnknownEffect on nil coordinator execute, got %v", err)
	}
	if _, err := nilCoord.ReconcileStorageAction(context.Background(), "a", "op"); !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("expected ErrUnknownEffect on nil coordinator reconcile, got %v", err)
	}

	coordNilParts := NewCoordinator(nil, nil)
	if _, err := coordNilParts.ExecuteStorageAction(context.Background(), "a", &actionv1.StorageMutationRequest{}); !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("expected ErrUnknownEffect on coord with nil parts, got %v", err)
	}
	if _, err := coordNilParts.ReconcileStorageAction(context.Background(), "a", "op"); !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("expected ErrUnknownEffect on coord with nil parts reconcile, got %v", err)
	}

	if _, err := coord.ExecuteStorageAction(context.Background(), "a", nil); !errors.Is(err, internalprotocol.ErrEmptyActionID) {
		t.Fatalf("expected ErrEmptyActionID on nil request, got %v", err)
	}
}

func TestExecuteActionStoreGetErrorAndNotFound(t *testing.T) {
	st := newFakeStore()
	st.getErr = errors.New("db disk failure")
	coord := NewCoordinator(st, &fakeWorkerClient{}, WithNow(func() int64 { return 500 }))

	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if err == nil || err.Error() != "db disk failure" {
		t.Fatalf("expected db disk failure, got %v", err)
	}

	st.getErr = nil
	_, err = coord.ExecuteStorageAction(context.Background(), "act-nonexistent", storageRequest("op-1"))
	if !errors.Is(err, ErrActionNotFound) {
		t.Fatalf("expected ErrActionNotFound, got %v", err)
	}
}

func TestExecuteActionLeaseExpiredOrTokenZero(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "PENDING",
	}

	// Lease expired
	st.lease.LeaseUntil = 400
	coord := NewCoordinator(st, &fakeWorkerClient{}, WithNow(func() int64 { return 500 }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if !errors.Is(err, ErrWriterLeaseLost) {
		t.Fatalf("expected ErrWriterLeaseLost when lease expired, got %v", err)
	}

	// Fencing token zero
	st.lease.LeaseUntil = 1000
	st.lease.FencingToken = 0
	_, err = coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if !errors.Is(err, ErrWriterLeaseLost) {
		t.Fatalf("expected ErrWriterLeaseLost when token zero, got %v", err)
	}
}

func TestExecuteActionFenceMismatch(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "PENDING",
	}
	coord := NewCoordinator(st, &fakeWorkerClient{}, WithNow(func() int64 { return 500 }))

	// Mismatched Action ID
	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", &actionv1.StorageMutationRequest{
		OperationId: "op-1",
		Fence:       &commonv1.ActionFence{ActionId: "other-act", ExecutionEpoch: 1},
	})
	if !errors.Is(err, internalprotocol.ErrFenceMismatch) {
		t.Fatalf("expected ErrFenceMismatch on action ID mismatch, got %v", err)
	}

	// Mismatched Execution Epoch
	_, err = coord.ExecuteStorageAction(context.Background(), "act-1", &actionv1.StorageMutationRequest{
		OperationId: "op-1",
		Fence:       &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 99},
	})
	if !errors.Is(err, internalprotocol.ErrFenceMismatch) {
		t.Fatalf("expected ErrFenceMismatch on epoch mismatch, got %v", err)
	}
}

func TestExecuteActionInvalidInitialStateAndPutErrors(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-invalid"] = store.Record{
		Domain:         "action",
		ID:             "act-invalid",
		ExecutionEpoch: 1,
		State:          "COMPLETED_UNKNOWN",
	}
	coord := NewCoordinator(st, &fakeWorkerClient{}, WithNow(func() int64 { return 500 }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-invalid", storageRequest("op-1"))
	if !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("expected ErrUnknownEffect on invalid initial state, got %v", err)
	}

	// PENDING -> CLAIMED put error
	st.records["action:act-pending"] = store.Record{
		Domain:         "action",
		ID:             "act-pending",
		ExecutionEpoch: 1,
		State:          "PENDING",
	}
	st.putErr = errors.New("put error on claim")
	_, err = coord.ExecuteStorageAction(context.Background(), "act-pending", storageRequest("op-1"))
	if err == nil || err.Error() != "put error on claim" {
		t.Fatalf("expected put error on claim, got %v", err)
	}

	// CLAIMED -> EXECUTING put error
	st.records["action:act-claimed"] = store.Record{
		Domain:         "action",
		ID:             "act-claimed",
		ExecutionEpoch: 1,
		State:          "CLAIMED",
	}
	st.putErr = errors.New("put error on executing")
	_, err = coord.ExecuteStorageAction(context.Background(), "act-claimed", storageRequest("op-1"))
	if err == nil || err.Error() != "put error on executing" {
		t.Fatalf("expected put error on executing, got %v", err)
	}
}

func TestExecuteActionEpochSupersededPostDispatch(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "PENDING",
	}

	worker := &fakeWorkerClient{
		executeResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
		},
		onExecute: func() {
			// Another coordinator or failover installed a higher epoch
			st.records["action:act-1"] = store.Record{
				Domain:         "action",
				ID:             "act-1",
				ExecutionEpoch: 2,
				State:          "PENDING",
			}
		},
	}

	coord := NewCoordinator(st, worker, WithNow(func() int64 { return 500 }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if !errors.Is(err, ErrActionExecutionStale) {
		t.Fatalf("expected ErrActionExecutionStale, got %v", err)
	}

	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "EFFECT_UNKNOWN" {
		t.Fatalf("expected transition to EFFECT_UNKNOWN, got %s", rec.State)
	}
}

func TestExecuteActionPutErrorsPostDispatch(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "PENDING",
	}

	// 1. Confirmed outcome but store Put fails
	workerConfirmed := &fakeWorkerClient{
		executeResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
		},
		onExecute: func() {
			st.putErr = errors.New("post-dispatch put fail")
		},
	}
	coord := NewCoordinator(st, workerConfirmed, WithNow(func() int64 { return 500 }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-1", storageRequest("op-1"))
	if err == nil || err.Error() != "post-dispatch put fail" {
		t.Fatalf("expected post-dispatch put fail, got %v", err)
	}

	// 2. Definite failure outcome but store Put fails
	st.putErr = nil
	st.records["action:act-2"] = store.Record{
		Domain:         "action",
		ID:             "act-2",
		ExecutionEpoch: 1,
		State:          "PENDING",
	}
	workerDefinite := &fakeWorkerClient{
		executeErr: internalprotocol.ErrStoragePreconditionRejected,
		onExecute: func() {
			st.putErr = errors.New("post-failure put fail")
		},
	}
	coord = NewCoordinator(st, workerDefinite, WithNow(func() int64 { return 500 }))
	_, err = coord.ExecuteStorageAction(context.Background(), "act-2", storageRequest("op-2"))
	if err == nil || err.Error() != "post-failure put fail" {
		t.Fatalf("expected post-failure put fail, got %v", err)
	}

	// 3. Uncertain outcome but store Put fails
	st.putErr = nil
	st.records["action:act-3"] = store.Record{
		Domain:         "action",
		ID:             "act-3",
		ExecutionEpoch: 1,
		State:          "PENDING",
	}
	workerUncertain := &fakeWorkerClient{
		executeErr: errors.New("network timeout"),
		onExecute: func() {
			st.putErr = errors.New("post-uncertain put fail")
		},
	}
	coord = NewCoordinator(st, workerUncertain, WithNow(func() int64 { return 500 }))
	_, err = coord.ExecuteStorageAction(context.Background(), "act-3", storageRequest("op-3"))
	if err == nil || err.Error() != "post-uncertain put fail" {
		t.Fatalf("expected post-uncertain put fail, got %v", err)
	}
}

func TestReconcileStorageActionMoreEdgeCases(t *testing.T) {
	st := newFakeStore()
	st.seedDispatch("act-1", "op-1")
	st.seedDispatch("act-exec", "op-1")

	// 1. Get error
	st.getErr = errors.New("get store failed")
	coord := NewCoordinator(st, &fakeWorkerClient{}, WithNow(func() int64 { return 500 }))
	_, err := coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if err == nil || err.Error() != "get store failed" {
		t.Fatalf("expected get store failed, got %v", err)
	}
	st.getErr = nil

	// 2. Action not found
	_, err = coord.ReconcileStorageAction(context.Background(), "act-missing", "op-1")
	if !errors.Is(err, ErrActionNotFound) {
		t.Fatalf("expected ErrActionNotFound, got %v", err)
	}

	// 3. Lease expired before query
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}
	st.lease.LeaseUntil = 400
	_, err = coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if !errors.Is(err, ErrWriterLeaseLost) {
		t.Fatalf("expected ErrWriterLeaseLost, got %v", err)
	}

	// 4. Invalid state for reconcile (PENDING)
	st.lease.LeaseUntil = 1000
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "PENDING",
	}
	_, err = coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("expected ErrUnknownEffect for PENDING reconcile, got %v", err)
	}

	// 5. EXECUTING transitions to EFFECT_UNKNOWN with put error
	st.records["action:act-exec"] = store.Record{
		Domain:         "action",
		ID:             "act-exec",
		ExecutionEpoch: 1,
		State:          "EXECUTING",
	}
	st.putErr = errors.New("executing transition fail")
	_, err = coord.ReconcileStorageAction(context.Background(), "act-exec", "op-1")
	if err == nil || err.Error() != "executing transition fail" {
		t.Fatalf("expected executing transition fail, got %v", err)
	}
	st.putErr = nil

	// 6. Post-query lease lost
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}
	timeVal := int64(500)
	worker := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
		},
		onQuery: func() {
			timeVal = 1200
		},
	}
	coordTime := NewCoordinator(st, worker, WithNow(func() int64 { return timeVal }))
	_, err = coordTime.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if !errors.Is(err, ErrWriterLeaseLost) {
		t.Fatalf("expected ErrWriterLeaseLost post-query, got %v", err)
	}

	// 7. Post-query epoch stale
	workerEpoch := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
		},
		onQuery: func() {
			st.records["action:act-1"] = store.Record{
				Domain:         "action",
				ID:             "act-1",
				ExecutionEpoch: 3,
				State:          "EFFECT_UNKNOWN",
			}
		},
	}
	coord = NewCoordinator(st, workerEpoch, WithNow(func() int64 { return 500 }))
	_, err = coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if !errors.Is(err, ErrActionExecutionStale) {
		t.Fatalf("expected ErrActionExecutionStale post-query, got %v", err)
	}

	// 8. A query/storage failure is not proof that the prior effect was absent.
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}
	workerFailed := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED,
			Error:  &commonv1.ErrorDetail{Code: "DISK_CORRUPT"},
		},
	}
	coord = NewCoordinator(st, workerFailed, WithNow(func() int64 { return 500 }))
	_, err = coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("query failure must remain uncertain: %v", err)
	}
	rec, _, _ := st.Get("action", "act-1")
	if rec.State != "EFFECT_UNKNOWN" {
		t.Fatalf("expected EFFECT_UNKNOWN, got %s", rec.State)
	}

	// 9. Reconcile query response CONFIRMED but put fails
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}
	workerConfirmed := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
			Fence:       &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1},
			OperationId: "op-1",
		},
		onQuery: func() {
			st.putErr = errors.New("reconcile confirmed put error")
		},
	}
	coord = NewCoordinator(st, workerConfirmed, WithNow(func() int64 { return 500 }))
	_, err = coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if err == nil || err.Error() != "reconcile confirmed put error" {
		t.Fatalf("expected put error, got %v", err)
	}
	st.putErr = nil

	// 10. Reconcile query response REJECTED but put fails
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}
	workerRejected := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED,
			State:       commonv1.EffectState_EFFECT_STATE_NOT_APPLIED,
			Fence:       &commonv1.ActionFence{ActionId: "act-1", ExecutionEpoch: 1},
			OperationId: "op-1",
			Error:       &commonv1.ErrorDetail{Code: "Rejected"},
		},
		onQuery: func() {
			st.putErr = errors.New("reconcile rejected put error")
		},
	}
	coord = NewCoordinator(st, workerRejected, WithNow(func() int64 { return 500 }))
	_, err = coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if err == nil || err.Error() != "reconcile rejected put error" {
		t.Fatalf("expected put error, got %v", err)
	}
	st.putErr = nil

	// 11. Reconcile query response EFFECT_UNKNOWN with nil queryErr
	st.records["action:act-1"] = store.Record{
		Domain:         "action",
		ID:             "act-1",
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}
	workerStillUnknown := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN,
		},
	}
	coord = NewCoordinator(st, workerStillUnknown, WithNow(func() int64 { return 500 }))
	_, err = coord.ReconcileStorageAction(context.Background(), "act-1", "op-1")
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("expected ErrStorageMutationUncertain, got %v", err)
	}
}

func TestDefiniteFailureBeforeEffect(t *testing.T) {
	definiteErrors := []error{
		internalprotocol.ErrStoragePreconditionRejected,
		internalprotocol.ErrFenceMismatch,
		internalprotocol.ErrEmptyActionID,
		internalprotocol.ErrZeroEpoch,
		internalprotocol.ErrStaleEpoch,
		internalprotocol.ErrServiceAuthenticationUnavailable,
		internalprotocol.ErrAuthenticationMissing,
		internalprotocol.ErrAuthenticationInvalid,
		internalprotocol.ErrStorageWorkerWithoutAuthority,
		internalprotocol.ErrStorageTargetMismatch,
		internalprotocol.ErrStorageDigestMismatch,
	}

	for _, err := range definiteErrors {
		if !isDefiniteFailureBeforeEffect(err) {
			t.Fatalf("expected %v to be definite failure before effect", err)
		}
	}

	if isDefiniteFailureBeforeEffect(nil) {
		t.Fatal("nil error should not be definite failure")
	}

	if isDefiniteFailureBeforeEffect(errors.New("arbitrary network error")) {
		t.Fatal("arbitrary error should not be definite failure")
	}
}

func TestReconcileStorageActionEdgeCases(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-edge"] = store.Record{
		Domain:         "action",
		ID:             "act-edge",
		ExecutionEpoch: 1,
		State:          "EFFECT_UNKNOWN",
	}
	workerQueryErr := &fakeWorkerClient{
		queryResp: &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED,
		},
		queryErr: errors.New("network dropped"),
	}
	st.seedDispatch("act-edge", "op-1")
	coord := NewCoordinator(st, workerQueryErr, WithNow(func() int64 { return 500 }))
	_, err := coord.ReconcileStorageAction(context.Background(), "act-edge", "op-1")
	if err == nil || err.Error() != "network dropped" {
		t.Fatalf("expected network dropped, got %v", err)
	}
}

func TestExecuteStorageActionPutError(t *testing.T) {
	st := newFakeStore()
	st.records["action:act-put-err"] = store.Record{
		Domain:         "action",
		ID:             "act-put-err",
		ExecutionEpoch: 1,
		State:          "CLAIMED",
	}
	st.seedDispatch("act-put-err", "op-1")
	workerSuccess := &fakeWorkerClient{
		executeResp: &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			OperationId: "op-1",
			State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
			Fence:       &commonv1.ActionFence{ActionId: "act-put-err", ExecutionEpoch: 1},
		},
	}
	st.putErr = errors.New("injected put failure")
	coord := NewCoordinator(st, workerSuccess, WithNow(func() int64 { return 500 }))
	_, err := coord.ExecuteStorageAction(context.Background(), "act-put-err", storageRequest("op-1"))
	if err == nil || err.Error() != "injected put failure" {
		t.Fatalf("expected injected put failure, got %v", err)
	}
}
