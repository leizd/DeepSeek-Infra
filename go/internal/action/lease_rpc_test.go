package action

import (
	"context"
	"encoding/json"
	"errors"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// leaseOnlyStore implements only the lease surface callWithActionLease uses.
type leaseOnlyStore struct {
	ControlStore
	lease store.WriterLease
	renew func(store.ActionLeaseRenewal) (store.ActionLease, error)
}

func (s *leaseOnlyStore) Writer() store.WriterLease { return s.lease }

func (s *leaseOnlyStore) RenewActionLease(request store.ActionLeaseRenewal) (store.ActionLease, error) {
	if s.renew != nil {
		return s.renew(request)
	}
	return store.ActionLease{ActionID: request.ActionID, Epoch: request.Epoch, LeaseUntil: 100000}, nil
}

// The remaining leasedControlStore methods belong to settlement, which this
// fixture deliberately never reaches.
func (s *leaseOnlyStore) ClaimLeasedStorageDispatch(store.Record, store.StorageDispatchIntent, string) error {
	return errLeaseFixtureUnused
}

func (s *leaseOnlyStore) GetLeasedStorageDispatch(string, uint64, string) (store.StorageDispatch, bool, error) {
	return store.StorageDispatch{}, false, errLeaseFixtureUnused
}

func (s *leaseOnlyStore) MarkActionVerifying(string, uint64, string, json.RawMessage) (store.Record, error) {
	return store.Record{}, errLeaseFixtureUnused
}

func (s *leaseOnlyStore) FailAction(string, uint64, string, json.RawMessage) (store.Record, error) {
	return store.Record{}, errLeaseFixtureUnused
}

func (s *leaseOnlyStore) MarkActionEffectUnknown(string, uint64, string) (store.Record, error) {
	return store.Record{}, errLeaseFixtureUnused
}

var errLeaseFixtureUnused = errors.New("lease fixture method is not part of this test")

func TestCallWithActionLeaseRefusesCancelledAndExpiredOwnership(t *testing.T) {
	claim := store.ActionLease{ActionID: "act-1", Epoch: 1, UpdatedAt: 0, LeaseUntil: 50000}
	renewal := store.ActionLeaseRenewal{ActionID: "act-1", Epoch: 1}
	called := 0
	call := func(context.Context) (*actionv1.StorageMutationResponse, error) {
		called++
		return &actionv1.StorageMutationResponse{}, nil
	}

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	owner := &leaseOnlyStore{lease: store.WriterLease{LeaseUntil: 100000}}
	coordinator := &Coordinator{now: func() int64 { return 1000 }}
	if _, _, err := coordinator.callWithActionLease(cancelled, owner, claim, renewal, call); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled caller: %v", err)
	}
	if called != 0 {
		t.Fatal("cancelled caller still reached the worker")
	}

	expired := &leaseOnlyStore{lease: store.WriterLease{LeaseUntil: 1000}}
	coordinator = &Coordinator{now: func() int64 { return 3000 }}
	if _, _, err := coordinator.callWithActionLease(context.Background(), expired, claim, renewal, call); !errors.Is(err, store.ErrActionLeaseExpired) {
		t.Fatalf("expired writer lease: %v", err)
	}
	if called != 0 {
		t.Fatal("expired ownership still reached the worker")
	}

	// A zero heartbeat interval means "use the default", and the call must still
	// complete and renew under a live lease.
	coordinator = &Coordinator{now: func() int64 { return 50000 }}
	liveClaim := store.ActionLease{ActionID: "act-1", Epoch: 1, UpdatedAt: 0, LeaseUntil: 90000}
	live := &leaseOnlyStore{lease: store.WriterLease{LeaseUntil: 100000}}
	if _, rpcErr, leaseErr := coordinator.callWithActionLease(context.Background(), live, liveClaim, renewal, call); rpcErr != nil || leaseErr != nil {
		t.Fatalf("live lease call: rpc=%v lease=%v", rpcErr, leaseErr)
	}
	if called != 1 {
		t.Fatalf("live lease call reached the worker %d times", called)
	}
}
