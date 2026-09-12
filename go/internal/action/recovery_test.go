package action

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

type recoveryQuery struct {
	executeCalls int
	queryCalls   int
	fence        *commonv1.ActionFence
	operation    string
	resp         *actionv1.StorageMutationResponse
	err          error
	onQuery      func()
}

func (w *recoveryQuery) ExecuteStorageMutation(context.Context, *actionv1.StorageMutationRequest, string) (*actionv1.StorageMutationResponse, error) {
	w.executeCalls++
	return nil, errors.New("recovery must not execute a write")
}

func (w *recoveryQuery) QueryStorageEffect(_ context.Context, fence *commonv1.ActionFence, operation, _ string) (*actionv1.StorageMutationResponse, error) {
	w.queryCalls++
	if fence != nil {
		w.fence = proto.Clone(fence).(*commonv1.ActionFence)
	}
	w.operation = operation
	if w.onQuery != nil {
		w.onQuery()
	}
	return w.resp, w.err
}

func reconcilingClaim(t *testing.T, takeovers int) (*store.Control, store.ActionLease, store.StorageDispatch, *atomic.Int64) {
	t.Helper()
	control, claim, now := nativeClaim(t)
	record, exists, err := control.Get("action", claim.ActionID)
	if err != nil || !exists {
		t.Fatalf("claimed action missing: %v", err)
	}
	intent, err := storageDispatchIntent(storageRequest("original-operation"), record)
	if err != nil {
		t.Fatal(err)
	}
	record.Revision++
	record.State = "EXECUTING"
	if err := control.ClaimLeasedStorageDispatch(record, intent, claim.ClaimToken); err != nil {
		t.Fatal(err)
	}
	original, bound, err := control.GetStorageDispatch(claim.ActionID, claim.Epoch)
	if err != nil || !bound {
		t.Fatalf("original dispatch missing: %v", err)
	}
	for i := 0; i < takeovers; i++ {
		now.Store(claim.LeaseUntil + 1)
		next, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: claim.ActionID, Owner: "successor", LeaseSeconds: 10})
		if err != nil || next.Record.State != "RECONCILING" {
			t.Fatalf("takeover %d: %v", i, err)
		}
		claim = next.Lease
	}
	return control, claim, original, now
}

func originalQueryResult(original store.StorageDispatch, status actionv1.StorageMutationStatus, state commonv1.EffectState, code string) *actionv1.StorageMutationResponse {
	resp := &actionv1.StorageMutationResponse{
		Status: status, State: state,
		Fence:       &commonv1.ActionFence{ActionId: original.Intent.ActionID, ExecutionEpoch: original.Intent.ExecutionEpoch},
		OperationId: original.Intent.OperationID, Etag: "\"etag-original\"", EffectId: "effect-original",
	}
	if code != "" {
		resp.Error = &commonv1.ErrorDetail{Code: code}
	}
	return resp
}

func TestReconcileClaimedUsesOriginalDispatchIdentity(t *testing.T) {
	control, claim, original, now := reconcilingClaim(t, 2)
	if claim.Epoch == original.Intent.ExecutionEpoch {
		t.Fatal("fixture did not advance the claim epoch")
	}
	worker := &recoveryQuery{resp: originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, "")}
	resources, err := control.GetResourceLeases(claim.ActionID)
	if err != nil || len(resources) == 0 {
		t.Fatalf("expected live reservations: %v", err)
	}
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	resp, err := coordinator.ReconcileClaimedStorageAction(context.Background(), claim, original.Intent.OperationID)
	if err != nil || resp == nil || resp.Etag != "\"etag-original\"" {
		t.Fatalf("applied recovery: resp=%v err=%v", resp, err)
	}
	if worker.executeCalls != 0 || worker.queryCalls != 1 {
		t.Fatalf("write or extra query: execute=%d query=%d", worker.executeCalls, worker.queryCalls)
	}
	if worker.operation != original.Intent.OperationID || worker.fence == nil || worker.fence.ExecutionEpoch != original.Intent.ExecutionEpoch || worker.fence.ExecutionEpoch == claim.Epoch {
		t.Fatalf("query used current claim epoch: fence=%+v claim=%d original=%d", worker.fence, claim.Epoch, original.Intent.ExecutionEpoch)
	}
	record, _, err := control.Get("action", claim.ActionID)
	if err != nil || record.State != "SUCCEEDED" || record.ExecutionEpoch != claim.Epoch {
		t.Fatalf("expected same-epoch SUCCEEDED: %+v %v", record, err)
	}
	got, bound, err := control.GetStorageDispatch(claim.ActionID, original.Intent.ExecutionEpoch)
	if err != nil || !bound || got != original {
		t.Fatalf("recovery rebound original dispatch: %v", err)
	}
	if _, bound, err := control.GetStorageDispatch(claim.ActionID, claim.Epoch); err != nil || bound {
		t.Fatalf("recovery claimed current epoch: bound=%v err=%v", bound, err)
	}
	after, err := control.GetResourceLeases(claim.ActionID)
	if err != nil || len(after) != 0 {
		t.Fatalf("success kept reservations: %v", err)
	}
}

func TestReconcileClaimedClassifiesBoundEvidenceWithoutNewWrite(t *testing.T) {
	for _, tc := range []struct {
		name     string
		status   actionv1.StorageMutationStatus
		state    commonv1.EffectState
		code     string
		queryErr error
		want     string
		locks    bool
		err      error
	}{
		{"applied", actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, "", nil, "SUCCEEDED", false, nil},
		{"recorded no effect", actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED, commonv1.EffectState_EFFECT_STATE_NOT_APPLIED, "PRECONDITION_REJECTED", nil, "FAILED_BEFORE_EFFECT", false, nil},
		{"recorded failed", actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED, commonv1.EffectState_EFFECT_STATE_NOT_APPLIED, "Failed", nil, "FAILED_BEFORE_EFFECT", false, nil},
		{"worker reconciling", actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_RECONCILING, commonv1.EffectState_EFFECT_STATE_UNKNOWN, "", internalprotocol.ErrUnknownEffect, "RECONCILING", true, ErrStorageMutationUncertain},
		{"effect unknown", actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN, commonv1.EffectState_EFFECT_STATE_UNKNOWN, "", internalprotocol.ErrUnknownEffect, "EFFECT_UNKNOWN", true, ErrStorageMutationUncertain},
		{"confirmed with error", actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, "", errors.New("transport after body"), "EFFECT_UNKNOWN", true, ErrStorageMutationUncertain},
		{"ambiguous failure", actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED, commonv1.EffectState_EFFECT_STATE_UNKNOWN, "DISK_CORRUPT", errors.New("disk"), "EFFECT_UNKNOWN", true, ErrStorageMutationUncertain},
	} {
		t.Run(tc.name, func(t *testing.T) {
			control, claim, original, now := reconcilingClaim(t, 1)
			worker := &recoveryQuery{
				resp: originalQueryResult(original, tc.status, tc.state, tc.code),
				err:  tc.queryErr,
			}
			coordinator := NewCoordinator(control, worker, WithNow(now.Load))
			_, err := coordinator.ReconcileClaimedStorageAction(context.Background(), claim, "")
			if tc.err == nil && err != nil {
				t.Fatal(err)
			}
			if tc.err != nil && !errors.Is(err, tc.err) {
				t.Fatalf("err=%v want=%v", err, tc.err)
			}
			if worker.executeCalls != 0 {
				t.Fatal("classified recovery executed a write")
			}
			record, _, err := control.Get("action", claim.ActionID)
			if err != nil || record.State != tc.want || record.ExecutionEpoch != claim.Epoch {
				t.Fatalf("state=%s epoch=%d want=%s err=%v", record.State, record.ExecutionEpoch, tc.want, err)
			}
			resources, err := control.GetResourceLeases(claim.ActionID)
			if err != nil || (len(resources) == 0) == tc.locks {
				t.Fatalf("locks=%d want kept=%v err=%v", len(resources), tc.locks, err)
			}
			got, bound, err := control.GetStorageDispatch(claim.ActionID, original.Intent.ExecutionEpoch)
			if err != nil || !bound || got.Intent.OperationID != original.Intent.OperationID {
				t.Fatalf("original dispatch changed: %v", err)
			}
		})
	}
}

func TestReconcileClaimedFailClosedMissingIdentityAndRetarget(t *testing.T) {
	for _, fault := range []string{"unbound", "wrong operation", "mismatch fence", "transport nil"} {
		t.Run(fault, func(t *testing.T) {
			control, claim, now := nativeClaim(t)
			var original store.StorageDispatch
			if fault != "unbound" {
				var dispatched store.ActionLease
				control, dispatched, original, now = reconcilingClaim(t, 1)
				claim = dispatched
			} else {
				now.Store(claim.LeaseUntil + 1)
				next, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: claim.ActionID, Owner: "successor", LeaseSeconds: 10})
				if err != nil {
					t.Fatal(err)
				}
				claim = next.Lease
			}
			worker := &recoveryQuery{}
			asserted := ""
			switch fault {
			case "wrong operation":
				asserted = "not-the-original"
			case "mismatch fence":
				worker.resp = originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, "")
				worker.resp.Fence = &commonv1.ActionFence{ActionId: claim.ActionID, ExecutionEpoch: claim.Epoch}
			case "transport nil":
				worker.err = errors.New("query transport lost")
			}
			before, _, err := control.Get("action", claim.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			resources, err := control.GetResourceLeases(claim.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			coordinator := NewCoordinator(control, worker, WithNow(now.Load))
			_, err = coordinator.ReconcileClaimedStorageAction(context.Background(), claim, asserted)
			if fault == "unbound" || fault == "wrong operation" {
				if !errors.Is(err, ErrStorageDispatchUnbound) || worker.queryCalls != 0 || worker.executeCalls != 0 {
					t.Fatalf("identity fault queried or wrote: err=%v query=%d execute=%d", err, worker.queryCalls, worker.executeCalls)
				}
			} else if !errors.Is(err, ErrStorageMutationUncertain) || worker.executeCalls != 0 {
				t.Fatalf("ambiguous evidence settled a write: err=%v execute=%d", err, worker.executeCalls)
			}
			after, _, err := control.Get("action", claim.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			if fault == "unbound" || fault == "wrong operation" {
				if after.State != before.State || after.Revision != before.Revision {
					t.Fatalf("fail-closed identity mutated journal: %+v", after)
				}
			} else if after.State != "EFFECT_UNKNOWN" || after.ExecutionEpoch != claim.Epoch {
				t.Fatalf("ambiguous evidence did not stay uncertain: %+v", after)
			}
			afterResources, err := control.GetResourceLeases(claim.ActionID)
			if err != nil || len(afterResources) != len(resources) {
				t.Fatalf("fail-closed recovery released locks: %v", err)
			}
		})
	}
}

func TestReconcileClaimedRejectsAuthorityAndInvalidClaims(t *testing.T) {
	for _, fault := range []string{"nil coordinator", "nil ctx", "canceled", "authoritative", "generic store", "empty owner", "writer token", "missing action", "stale epoch", "claimed state", "closed"} {
		t.Run(fault, func(t *testing.T) {
			control, claim, original, now := reconcilingClaim(t, 1)
			worker := &recoveryQuery{resp: originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, "")}
			coordinator := NewCoordinator(control, worker, WithNow(now.Load))
			ctx := context.Background()
			want := internalprotocol.ErrUnknownEffect
			switch fault {
			case "nil coordinator":
				coordinator = nil
			case "nil ctx":
				ctx = nil
			case "canceled":
				var cancel context.CancelFunc
				ctx, cancel = context.WithCancel(ctx)
				cancel()
				want = context.Canceled
			case "authoritative":
				coordinator = NewCoordinator(control, worker, WithNow(now.Load), WithAuthoritative(true))
				want = store.ErrCutoverNotAuthorized
			case "generic store":
				coordinator = NewCoordinator(newFakeStore(), worker, WithNow(now.Load))
				want = store.ErrActionLeaseRequired
			case "empty owner":
				claim.Owner = ""
				want = store.ErrActionLeaseStale
			case "writer token":
				claim.WriterFencingToken++
				want = store.ErrActionLeaseStale
			case "missing action":
				claim.ActionID = "missing-action"
				want = ErrActionNotFound
			case "stale epoch":
				claim.Epoch++
				want = ErrActionExecutionStale
			case "claimed state":
				control, claim, now = nativeClaim(t)
				coordinator = NewCoordinator(control, worker, WithNow(now.Load))
			case "closed":
				if err := control.Close(); err != nil {
					t.Fatal(err)
				}
				want = store.ErrWriterFenceHeld
			}
			_, err := coordinator.ReconcileClaimedStorageAction(ctx, claim, "")
			if !errors.Is(err, want) {
				t.Fatalf("err=%v want=%v", err, want)
			}
			if worker.executeCalls != 0 || (fault != "closed" && worker.queryCalls != 0) {
				t.Fatalf("invalid claim queried or wrote: query=%d execute=%d", worker.queryCalls, worker.executeCalls)
			}
		})
	}
}

func TestReconcileClaimedWriterLossAfterQueryDoesNotSettle(t *testing.T) {
	control, claim, original, now := reconcilingClaim(t, 1)
	deadline := control.Writer().LeaseUntil
	worker := &recoveryQuery{
		resp: originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, ""),
		onQuery: func() {
			now.Store(deadline)
		},
	}
	before, _, err := control.Get("action", claim.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	if _, err := coordinator.ReconcileClaimedStorageAction(context.Background(), claim, ""); !errors.Is(err, store.ErrWriterFenceHeld) {
		t.Fatalf("lost writer settled: %v", err)
	}
	after, _, err := control.Get("action", claim.ActionID)
	if err != nil || after.State != before.State || after.Revision != before.Revision {
		t.Fatalf("lost writer mutated journal: %+v", after)
	}
	if worker.executeCalls != 0 {
		t.Fatal("lost writer executed a write")
	}
}

func TestReconcileStorageActionStillRejectsReconcilingTakeover(t *testing.T) {
	control, claim, original, now := reconcilingClaim(t, 1)
	worker := &recoveryQuery{resp: originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, "")}
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	if _, err := coordinator.ReconcileStorageAction(context.Background(), claim.ActionID, original.Intent.OperationID); !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("unbound reconciler accepted RECONCILING: %v", err)
	}
	if worker.queryCalls != 0 || worker.executeCalls != 0 {
		t.Fatal("legacy reconciler used current epoch as a write identity")
	}
	record, _, err := control.Get("action", claim.ActionID)
	if err != nil || record.State != "RECONCILING" {
		t.Fatalf("legacy reconciler mutated RECONCILING: %+v", record)
	}
}

func TestReconcileClaimedLookupAndSettlementFailuresStayClosed(t *testing.T) {
	for _, fault := range []string{"lookup token", "closed after query", "epoch advanced after query", "complete expired", "fail expired"} {
		t.Run(fault, func(t *testing.T) {
			control, claim, original, now := reconcilingClaim(t, 1)
			status := actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED
			state := commonv1.EffectState_EFFECT_STATE_APPLIED
			code := ""
			if fault == "fail expired" {
				status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED
				state = commonv1.EffectState_EFFECT_STATE_NOT_APPLIED
				code = "PRECONDITION_REJECTED"
			}
			worker := &recoveryQuery{resp: originalQueryResult(original, status, state, code)}
			want := store.ErrInvalidClaimToken
			switch fault {
			case "lookup token":
				claim.ClaimToken = "substituted-claim-token"
			case "closed after query":
				worker.onQuery = func() {
					if err := control.Close(); err != nil {
						t.Fatal(err)
					}
				}
				want = store.ErrWriterFenceHeld
			case "epoch advanced after query":
				worker.onQuery = func() {
					expireQueriedAction(t, control, claim.ActionID, now)
					if _, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: claim.ActionID, Owner: "later-successor", LeaseSeconds: 10}); err != nil {
						t.Fatal(err)
					}
				}
				want = store.ErrActionLeaseStale
			case "complete expired", "fail expired":
				worker.onQuery = func() { expireQueriedAction(t, control, claim.ActionID, now) }
				want = store.ErrActionLeaseExpired
			}
			coordinator := NewCoordinator(control, worker, WithNow(now.Load))
			_, err := coordinator.ReconcileClaimedStorageAction(context.Background(), claim, "")
			if !errors.Is(err, want) {
				t.Fatalf("err=%v want=%v", err, want)
			}
			if worker.executeCalls != 0 {
				t.Fatal("failure path executed a write")
			}
			if fault == "lookup token" && worker.queryCalls != 0 {
				t.Fatal("bad claim token reached the worker")
			}
			if fault == "epoch advanced after query" {
				record, _, err := control.Get("action", claim.ActionID)
				if err != nil || record.State != "RECONCILING" || record.ExecutionEpoch == claim.Epoch {
					t.Fatalf("stale claim settled after takeover: %+v %v", record, err)
				}
			}
			if fault == "complete expired" || fault == "fail expired" {
				record, _, err := control.Get("action", claim.ActionID)
				if err != nil || record.State != "RECONCILING" {
					t.Fatalf("expired settlement mutated journal: %+v %v", record, err)
				}
			}
		})
	}
}

func TestReconcileClaimedCurrentEpochCannotBindOriginalEffect(t *testing.T) {
	control, claim, original, now := reconcilingClaim(t, 2)
	if claim.Epoch == original.Intent.ExecutionEpoch {
		t.Fatal("fixture did not separate claim epoch from dispatch epoch")
	}
	worker := &epochTrapQuery{claimEpoch: claim.Epoch, original: original}
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	_, err := coordinator.ReconcileClaimedStorageAction(context.Background(), claim, "")
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("current-epoch trap settled: %v", err)
	}
	if worker.executeCalls != 0 {
		t.Fatal("current-epoch trap executed a write")
	}
	if worker.fence == nil || worker.fence.ExecutionEpoch != original.Intent.ExecutionEpoch || worker.operation != original.Intent.OperationID {
		t.Fatalf("query did not use original identity: fence=%+v op=%s", worker.fence, worker.operation)
	}
	record, _, err := control.Get("action", claim.ActionID)
	if err != nil || record.State != "EFFECT_UNKNOWN" || record.ExecutionEpoch != claim.Epoch {
		t.Fatalf("expected uncertain same-epoch recovery: %+v %v", record, err)
	}
	got, bound, err := control.GetStorageDispatch(claim.ActionID, original.Intent.ExecutionEpoch)
	if err != nil || !bound || got.Intent.OperationID != original.Intent.OperationID {
		t.Fatalf("trap rebound original dispatch: %v", err)
	}
	if _, bound, err := control.GetStorageDispatch(claim.ActionID, claim.Epoch); err != nil || bound {
		t.Fatalf("current claim epoch became a write identity: bound=%v err=%v", bound, err)
	}
}

type epochTrapQuery struct {
	claimEpoch   uint64
	original     store.StorageDispatch
	executeCalls int
	fence        *commonv1.ActionFence
	operation    string
}

func (w *epochTrapQuery) ExecuteStorageMutation(context.Context, *actionv1.StorageMutationRequest, string) (*actionv1.StorageMutationResponse, error) {
	w.executeCalls++
	return nil, errors.New("recovery must not execute a write")
}

func (w *epochTrapQuery) QueryStorageEffect(_ context.Context, fence *commonv1.ActionFence, operation, _ string) (*actionv1.StorageMutationResponse, error) {
	if fence != nil {
		w.fence = proto.Clone(fence).(*commonv1.ActionFence)
	}
	w.operation = operation
	if fence != nil && fence.ExecutionEpoch == w.claimEpoch {
		return &actionv1.StorageMutationResponse{
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			State:  commonv1.EffectState_EFFECT_STATE_APPLIED,
			Fence:  proto.Clone(fence).(*commonv1.ActionFence), OperationId: operation,
			Etag: "\"laundered\"", EffectId: "current-epoch-effect",
		}, nil
	}
	return originalQueryResult(w.original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_EFFECT_UNKNOWN, commonv1.EffectState_EFFECT_STATE_UNKNOWN, "EFFECT_UNKNOWN"), internalprotocol.ErrUnknownEffect
}

func TestReconcileClaimedDeadlineRenewalDoesNotMintWriteIdentity(t *testing.T) {
	control, claim, original, now := reconcilingClaim(t, 2)
	before, exists, err := control.GetActionLease(claim.ActionID)
	if err != nil || !exists {
		t.Fatalf("live claim missing before renewal: exists=%v err=%v", exists, err)
	}
	var renewedUntil int64
	worker := &recoveryQuery{
		resp: originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_RECONCILING, commonv1.EffectState_EFFECT_STATE_UNKNOWN, ""),
		err:  internalprotocol.ErrUnknownEffect,
		onQuery: func() {
			got, err := control.RenewActionLease(store.ActionLeaseRenewal{
				ActionID: claim.ActionID, Epoch: claim.Epoch, Owner: claim.Owner,
				ClaimToken: claim.ClaimToken, LeaseSeconds: 30,
			})
			if err != nil {
				t.Fatal(err)
			}
			if got.Epoch != claim.Epoch || got.ClaimToken != claim.ClaimToken {
				t.Fatalf("renewal minted claim identity: %+v", got)
			}
			renewedUntil = got.LeaseUntil
		},
	}
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	_, err = coordinator.ReconcileClaimedStorageAction(context.Background(), claim, original.Intent.OperationID)
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("renewed recovery settled a write: %v", err)
	}
	if worker.executeCalls != 0 || worker.fence == nil || worker.fence.ExecutionEpoch != original.Intent.ExecutionEpoch || worker.operation != original.Intent.OperationID {
		t.Fatalf("renewal retargeted the worker query: fence=%+v op=%s execute=%d", worker.fence, worker.operation, worker.executeCalls)
	}
	record, _, err := control.Get("action", claim.ActionID)
	if err != nil || record.State != "RECONCILING" || record.ExecutionEpoch != claim.Epoch {
		t.Fatalf("renewal changed recovery state: %+v %v", record, err)
	}
	got, bound, err := control.GetStorageDispatch(claim.ActionID, original.Intent.ExecutionEpoch)
	if err != nil || !bound || got != original {
		t.Fatalf("renewal rebound original dispatch: %v", err)
	}
	if _, bound, err := control.GetStorageDispatch(claim.ActionID, claim.Epoch); err != nil || bound {
		t.Fatalf("renewal created a current-epoch dispatch: bound=%v err=%v", bound, err)
	}
	lease, exists, err := control.GetActionLease(claim.ActionID)
	if err != nil || !exists || lease.Epoch != before.Epoch || lease.ClaimToken != before.ClaimToken || lease.LeaseUntil != renewedUntil || lease.LeaseUntil <= before.LeaseUntil {
		t.Fatalf("renewal lost live claim or minted identity: before=%+v after=%+v err=%v", before, lease, err)
	}
	locks, err := control.GetResourceLeases(claim.ActionID)
	if err != nil || len(locks) == 0 {
		t.Fatalf("renewed uncertain recovery released locks: %v", err)
	}
}

func TestReconcileClaimedUnknownAfterTakeoverKeepsOriginalIdentity(t *testing.T) {
	control, claim, original, now := reconcilingClaim(t, 1)
	if _, err := control.MarkActionEffectUnknown(claim.ActionID, claim.Epoch, claim.ClaimToken); err != nil {
		t.Fatal(err)
	}
	worker := &recoveryQuery{resp: originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, "")}
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	if _, err := coordinator.ReconcileClaimedStorageAction(context.Background(), claim, ""); err != nil {
		t.Fatal(err)
	}
	if worker.fence == nil || worker.fence.ExecutionEpoch != original.Intent.ExecutionEpoch {
		t.Fatalf("unknown recovery used claim epoch %d original %d", claim.Epoch, original.Intent.ExecutionEpoch)
	}
	record, _, err := control.Get("action", claim.ActionID)
	if err != nil || record.State != "SUCCEEDED" {
		t.Fatalf("expected SUCCEEDED: %+v %v", record, err)
	}
}
