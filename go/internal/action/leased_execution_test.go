package action

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// Actual Go SQLite lifecycle with an RPC test double. This is NOT provider proof.
type leasedRPC func(context.Context, *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error)

func (rpc leasedRPC) ExecuteStorageMutation(ctx context.Context, req *actionv1.StorageMutationRequest, _ string) (*actionv1.StorageMutationResponse, error) {
	return rpc(ctx, req)
}
func (rpc leasedRPC) QueryStorageEffect(context.Context, *commonv1.ActionFence, string, string) (*actionv1.StorageMutationResponse, error) {
	return nil, ErrStorageMutationUncertain
}

func nativeClaim(t *testing.T) (*store.Control, store.ActionLease, *atomic.Int64) {
	t.Helper()
	now := &atomic.Int64{}
	now.Store(1000)
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "native-coordinator", Now: now.Load})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	if err := control.Put(store.Record{Domain: "action", ID: "leased-action", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: "leased-action", LeaseSeconds: 10, ResourceKeys: []string{"reserved-target"}})
	if err != nil {
		t.Fatal(err)
	}
	return control, claim.Lease, now
}

func claimedResponse(req *actionv1.StorageMutationRequest) *actionv1.StorageMutationResponse {
	return &actionv1.StorageMutationResponse{Fence: req.Fence, OperationId: req.OperationId,
		Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, State: commonv1.EffectState_EFFECT_STATE_APPLIED}
}

// A claim that cannot be honoured is refused before anything is dispatched, and the refusal
// names the reason instead of reporting a mutation that never happened.
func TestClaimedExecutionRefusesWhatItCannotHonour(t *testing.T) {
	t.Run("a coordinator with no store", func(t *testing.T) {
		if _, err := (&Coordinator{}).ExecuteClaimedStorageAction(
			context.Background(), store.ActionLease{}, storageRequest("unbound-operation"),
		); !errors.Is(err, internalprotocol.ErrUnknownEffect) {
			t.Fatalf("a coordinator without a store must report an unknown effect: %v", err)
		}
	})

	t.Run("a store that cannot hold a native claim", func(t *testing.T) {
		// Embedding the interface is enough: the assertion for `leasedControlStore` is the point
		// of the call, and a store that only satisfies ControlStore must never receive an
		// unbound dispatch.
		worker := leasedRPC(func(context.Context, *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
			t.Fatal("the worker must not be called for a store that cannot hold a claim")
			return nil, nil
		})
		coordinator := NewCoordinator(controlStoreWithoutClaims{}, worker)
		if _, err := coordinator.ExecuteClaimedStorageAction(
			context.Background(), store.ActionLease{ActionID: "leased-action", Owner: "native-coordinator"},
			storageRequest("unbound-operation"),
		); !errors.Is(err, store.ErrActionLeaseRequired) {
			t.Fatalf("a store without native claims must be refused: %v", err)
		}
	})

	t.Run("an action whose effect is already unknown", func(t *testing.T) {
		control, claim, now := nativeClaim(t)
		if _, err := control.MarkActionEffectUnknown(claim.ActionID, claim.Epoch, claim.ClaimToken); err != nil {
			t.Fatal(err)
		}
		worker := leasedRPC(func(context.Context, *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
			t.Fatal("the worker must not be called once the action left CLAIMED")
			return nil, nil
		})
		coordinator := NewCoordinator(control, worker, WithNow(now.Load))
		if _, err := coordinator.ExecuteClaimedStorageAction(
			context.Background(), claim, storageRequest("late-operation"),
		); !errors.Is(err, ErrStorageMutationUncertain) {
			t.Fatalf("executing an action that is no longer claimed must be uncertain: %v", err)
		}
	})
}

type controlStoreWithoutClaims struct{ ControlStore }

func TestClaimedExecutionVerifiesAppliedAndSettlesOnlyRecordedNoEffect(t *testing.T) {
	for _, outcome := range []string{"confirmed", "lost ack", "wrong operation", "error alone", "recorded no effect"} {
		t.Run(outcome, func(t *testing.T) {
			control, claim, now := nativeClaim(t)
			worker := leasedRPC(func(_ context.Context, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
				if _, bound, err := control.GetStorageDispatch(claim.ActionID, claim.Epoch); err != nil || !bound {
					t.Fatalf("RPC ran before durable dispatch: %v", err)
				}
				if outcome == "lost ack" {
					return nil, errors.New("ack lost")
				}
				if outcome == "error alone" {
					return nil, internalprotocol.ErrStoragePreconditionRejected
				}
				resp := claimedResponse(req)
				if outcome == "wrong operation" {
					resp.OperationId = "other"
				}
				if outcome == "recorded no effect" {
					resp.Status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED
					resp.State = commonv1.EffectState_EFFECT_STATE_NOT_APPLIED
					resp.Error = &commonv1.ErrorDetail{Code: "PRECONDITION_REJECTED"}
				}
				return resp, nil
			})
			coordinator := NewCoordinator(control, worker, WithNow(now.Load))
			_, err := coordinator.ExecuteClaimedStorageAction(context.Background(), claim, storageRequest("leased-operation"))
			want := "EFFECT_UNKNOWN"
			terminal := outcome == "recorded no effect"
			if outcome == "confirmed" || terminal {
				want = "VERIFYING"
				if outcome == "recorded no effect" {
					want = "FAILED_BEFORE_EFFECT"
				}
				if err != nil {
					t.Fatal(err)
				}
			} else if !errors.Is(err, ErrStorageMutationUncertain) {
				t.Fatalf("uncertain result error: %v", err)
			}
			record, _, err := control.Get("action", claim.ActionID)
			if err != nil || record.State != want {
				t.Fatalf("state=%s want=%s err=%v", record.State, want, err)
			}
			resources, err := control.GetResourceLeases(claim.ActionID)
			if err != nil || (len(resources) == 0) != terminal {
				t.Fatalf("incorrect resource release: count=%d err=%v", len(resources), err)
			}
		})
	}
}

func TestClaimedExecutionLeaseLossCancelsRPCAndRejectsLateACK(t *testing.T) {
	control, claim, now := nativeClaim(t)
	worker := leasedRPC(func(ctx context.Context, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
		now.Store(1030) // Writer deadline reached while the RPC is outstanding.
		select {
		case <-ctx.Done():
			return claimedResponse(req), nil // Deliberately late ACK.
		case <-time.After(5 * time.Second):
			t.Error("renewal failure did not cancel RPC")
			return nil, errors.New("test timeout")
		}
	})
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	coordinator.leaseHeartbeatInterval = 5 * time.Millisecond
	_, err := coordinator.ExecuteClaimedStorageAction(context.Background(), claim, storageRequest("late-operation"))
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("lost lease error=%v", err)
	}
	record, _, err := control.Get("action", claim.ActionID)
	if err != nil || record.State != "EXECUTING" {
		t.Fatalf("late ACK changed lost-lease journal: state=%s err=%v", record.State, err)
	}
	resources, err := control.GetResourceLeases(claim.ActionID)
	if err != nil || len(resources) != 1 {
		t.Fatalf("lost lease released resources: %v", err)
	}
}

func TestClaimedExecutionRejectsInvalidClaimBeforeRPC(t *testing.T) {
	for _, invalid := range []string{"nil request", "request epoch", "request action", "claim epoch", "claim token", "empty owner", "writer", "expired", "missing action", "intent", "canceled", "authority"} {
		t.Run(invalid, func(t *testing.T) {
			defer func() {
				if recovered := recover(); recovered != nil {
					t.Fatalf("invalid input panicked: %v", recovered)
				}
			}()
			control, claim, now := nativeClaim(t)
			original := claim
			called := false
			worker := leasedRPC(func(_ context.Context, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
				called = true
				return claimedResponse(req), nil
			})
			coordinator := NewCoordinator(control, worker, WithNow(now.Load))
			req := storageRequest("invalid-input")
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var want error
			switch invalid {
			case "nil request":
				req, want = nil, store.ErrInvalidStorageIntent
			case "request epoch":
				req.Fence = &commonv1.ActionFence{ActionId: claim.ActionID, ExecutionEpoch: claim.Epoch + 1}
				want = internalprotocol.ErrFenceMismatch
			case "request action":
				req.Fence = &commonv1.ActionFence{ActionId: "other", ExecutionEpoch: claim.Epoch}
				want = internalprotocol.ErrFenceMismatch
			case "claim epoch":
				claim.Epoch++
				want = ErrActionExecutionStale
			case "claim token":
				claim.ClaimToken = "wrong"
				want = store.ErrInvalidClaimToken
			case "empty owner":
				claim.Owner = ""
				want = store.ErrActionLeaseStale
			case "writer":
				claim.WriterFencingToken++
				want = store.ErrActionLeaseStale
			case "expired":
				now.Store(1010)
				want = store.ErrActionLeaseExpired
			case "missing action":
				claim.ActionID = "absent"
				want = ErrActionNotFound
			case "intent":
				req.OperationId = ""
				want = store.ErrInvalidStorageIntent
			case "canceled":
				cancel()
				want = context.Canceled
			case "authority":
				coordinator.authoritative = true
				want = store.ErrCutoverNotAuthorized
			}
			_, err := coordinator.ExecuteClaimedStorageAction(ctx, claim, req)
			if called || !errors.Is(err, want) {
				t.Fatalf("RPC=%v err=%v want=%v", called, err, want)
			}
			after, exists, err := control.GetActionLease(original.ActionID)
			if err != nil || !exists || after != original {
				t.Fatalf("rejected input mutated claim: %v", err)
			}
		})
	}
}

func TestClaimedExecutionRenewsSQLiteClaimDuringRPC(t *testing.T) {
	control, claim, now := nativeClaim(t)
	worker := leasedRPC(func(ctx context.Context, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
		now.Store(1001)
		ticker := time.NewTicker(5 * time.Millisecond)
		defer ticker.Stop()
		timeout := time.NewTimer(5 * time.Second)
		defer timeout.Stop()
		for {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-timeout.C:
				t.Error("durable heartbeat was not observed")
				return nil, errors.New("test timeout")
			case <-ticker.C:
				lease, exists, err := control.GetActionLease(claim.ActionID)
				if err != nil {
					return nil, err
				}
				if exists && lease.UpdatedAt == 1001 {
					resources, err := control.GetResourceLeases(claim.ActionID)
					if err != nil || len(resources) != 1 || resources[0].LeaseUntil != lease.LeaseUntil || control.Writer().LeaseUntil != 1031 {
						t.Fatalf("heartbeat did not atomically renew resource/writer leases: %v", err)
					}
					return claimedResponse(req), nil
				}
			}
		}
	})
	coordinator := NewCoordinator(control, worker, WithNow(now.Load))
	coordinator.leaseHeartbeatInterval = 5 * time.Millisecond
	if _, err := coordinator.ExecuteClaimedStorageAction(context.Background(), claim, storageRequest("heartbeat-operation")); err != nil {
		t.Fatal(err)
	}
	if _, exists, err := control.GetActionLease(claim.ActionID); err != nil || !exists {
		t.Fatalf("APPLIED released the verification lease: %v", err)
	}
	if record, _, err := control.Get("action", claim.ActionID); err != nil || record.State != "VERIFYING" {
		t.Fatalf("heartbeat did not reach VERIFYING: %v", err)
	}
}

type cancelAfterClaimStore struct {
	*store.Control
	cancel context.CancelFunc
}

func (owner cancelAfterClaimStore) ClaimLeasedStorageDispatch(record store.Record, intent store.StorageDispatchIntent, token string) error {
	if err := owner.Control.ClaimLeasedStorageDispatch(record, intent, token); err != nil {
		return err
	}
	owner.cancel()
	return nil
}

func TestClaimedExecutionCanceledAfterIntentDoesNotStartRPC(t *testing.T) {
	control, claim, now := nativeClaim(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	called := false
	worker := leasedRPC(func(_ context.Context, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
		called = true
		return claimedResponse(req), nil
	})
	coordinator := NewCoordinator(cancelAfterClaimStore{control, cancel}, worker, WithNow(now.Load))
	if _, err := coordinator.ExecuteClaimedStorageAction(ctx, claim, storageRequest("cancel-after-intent")); !errors.Is(err, ErrStorageMutationUncertain) || called {
		t.Fatalf("canceled execution started RPC: called=%v err=%v", called, err)
	}
	record, _, err := control.Get("action", claim.ActionID)
	if err != nil || record.State != "EFFECT_UNKNOWN" {
		t.Fatalf("canceled intent lost uncertainty: %v", err)
	}
}

func TestClaimedExecutionHeartbeatFitsShortWriterLease(t *testing.T) {
	now := &atomic.Int64{}
	now.Store(1000)
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "short-writer", LeaseSeconds: 1, Now: now.Load})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	if err := control.Put(store.Record{Domain: "action", ID: "short-writer-action", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: "short-writer-action", LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	worker := leasedRPC(func(ctx context.Context, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
		now.Store(1001)
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(2 * time.Second):
			t.Error("fixed heartbeat outlived the short writer lease")
			return nil, errors.New("test timeout")
		}
	})
	_, err = NewCoordinator(control, worker, WithNow(now.Load)).ExecuteClaimedStorageAction(context.Background(), claim.Lease, storageRequest("short-writer-operation"))
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("short lease result: %v", err)
	}
}
