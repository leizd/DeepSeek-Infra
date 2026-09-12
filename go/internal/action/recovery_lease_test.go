package action

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

type recoveryRPC func(context.Context, *commonv1.ActionFence, string) (*actionv1.StorageMutationResponse, error)

// Keep the independent writer alive while the action's renewed deadline passes.
// Read the durable deadline, not the stale claim value captured before the RPC.
func expireQueriedAction(t *testing.T, control *store.Control, actionID string, now *atomic.Int64) {
	t.Helper()
	lease, exists, err := control.GetActionLease(actionID)
	if err != nil || !exists {
		t.Fatalf("missing queried claim: %v", err)
	}
	for control.Writer().LeaseUntil <= lease.LeaseUntil+1 {
		now.Store(control.Writer().LeaseUntil - 1)
		if err := control.RenewWriter(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	now.Store(lease.LeaseUntil + 1)
}

func (call recoveryRPC) QueryStorageEffect(ctx context.Context, fence *commonv1.ActionFence, operation, _ string) (*actionv1.StorageMutationResponse, error) {
	return call(ctx, fence, operation)
}

func (recoveryRPC) ExecuteStorageMutation(context.Context, *actionv1.StorageMutationRequest, string) (*actionv1.StorageMutationResponse, error) {
	return nil, errors.New("recovery must not execute a write")
}

func TestRecoveryQueryRenewsLiveSQLiteClaimAndCancelsOnLeaseLoss(t *testing.T) {
	for _, loseLease := range []bool{false, true} {
		t.Run(map[bool]string{false: "live renewal", true: "lease lost"}[loseLease], func(t *testing.T) {
			control, claim, original, now := reconcilingClaim(t, 1)
			worker := recoveryRPC(func(ctx context.Context, _ *commonv1.ActionFence, _ string) (*actionv1.StorageMutationResponse, error) {
				if loseLease {
					now.Store(control.Writer().LeaseUntil)
				} else {
					now.Add(1)
				}
				ticker := time.NewTicker(5 * time.Millisecond)
				defer ticker.Stop()
				timeout := time.NewTimer(5 * time.Second)
				defer timeout.Stop()
				for {
					select {
					case <-ctx.Done():
						// Even a late APPLIED body cannot outvote lost ownership.
						return originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, ""), nil
					case <-timeout.C:
						t.Error("query heartbeat did not renew or cancel")
						return nil, errors.New("test timeout")
					case <-ticker.C:
						lease, exists, err := control.GetActionLease(claim.ActionID)
						if err != nil {
							return nil, err
						}
						if !loseLease && exists && lease.UpdatedAt == now.Load() {
							locks, err := control.GetResourceLeases(claim.ActionID)
							if err != nil || len(locks) == 0 || locks[0].LeaseUntil != lease.LeaseUntil || lease.LeaseUntil <= claim.LeaseUntil {
								t.Fatalf("incomplete atomic renewal: %v", err)
							}
							return originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_RECONCILING, commonv1.EffectState_EFFECT_STATE_UNKNOWN, ""), nil
						}
					}
				}
			})
			coordinator := NewCoordinator(control, worker, WithNow(now.Load))
			coordinator.leaseHeartbeatInterval = 5 * time.Millisecond
			_, err := coordinator.ReconcileClaimedStorageAction(context.Background(), claim, "")
			if !errors.Is(err, ErrStorageMutationUncertain) {
				t.Fatalf("query did not retain uncertainty: %v", err)
			}
			if loseLease && !errors.Is(err, store.ErrWriterFenceHeld) {
				t.Fatalf("lease loss omitted: %v", err)
			}
			record, _, err := control.Get("action", claim.ActionID)
			if err != nil || record.State != "RECONCILING" {
				t.Fatalf("query settled without qualification: %v", err)
			}
		})
	}
}

func TestRecoveryCanceledQueryCannotSettleLateApplied(t *testing.T) {
	control, claim, original, now := reconcilingClaim(t, 1)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	worker := recoveryRPC(func(context.Context, *commonv1.ActionFence, string) (*actionv1.StorageMutationResponse, error) {
		cancel()
		return originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, ""), nil
	})
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	if _, err := coordinator.ReconcileClaimedStorageAction(ctx, claim, ""); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation lost to late applied: %v", err)
	}
	record, _, err := control.Get("action", claim.ActionID)
	if err != nil || record.State == "SUCCEEDED" {
		t.Fatalf("canceled query settled: %v", err)
	}
	locks, err := control.GetResourceLeases(claim.ActionID)
	if err != nil || len(locks) == 0 {
		t.Fatalf("canceled query released resources: %v", err)
	}
}

type cancelAfterRecoveryRenew struct {
	*store.Control
	cancel context.CancelFunc
	count  int
}

func (owner *cancelAfterRecoveryRenew) RenewActionLease(request store.ActionLeaseRenewal) (store.ActionLease, error) {
	lease, err := owner.Control.RenewActionLease(request)
	owner.count++
	if owner.count == 2 {
		owner.cancel()
	}
	return lease, err
}

func TestRecoveryCancellationDuringPostQueryRenewalCannotSettle(t *testing.T) {
	control, claim, original, now := reconcilingClaim(t, 1)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	owner := &cancelAfterRecoveryRenew{Control: control, cancel: cancel}
	worker := &recoveryQuery{resp: originalQueryResult(original, actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, commonv1.EffectState_EFFECT_STATE_APPLIED, "")}
	coordinator := NewCoordinator(owner, worker, WithNow(now.Load))
	if _, err := coordinator.ReconcileClaimedStorageAction(ctx, claim, ""); !errors.Is(err, context.Canceled) {
		t.Fatalf("post-query cancellation settled: %v", err)
	}
	locks, err := control.GetResourceLeases(claim.ActionID)
	if err != nil || len(locks) == 0 {
		t.Fatalf("post-query cancellation released resources: %v", err)
	}
}
