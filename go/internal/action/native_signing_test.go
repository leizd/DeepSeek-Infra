package action

import (
	"context"
	"errors"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

type nativeSigningWorker struct {
	fakeWorkerClient
	sign func(*actionv1.StorageMutationRequest, int64, int64) (*actionv1.StorageMutationRequest, error)
}

func (worker *nativeSigningWorker) AuthorizeStorage(_ context.Context, input *actionv1.StorageMutationRequest, revision, fence int64) (*actionv1.StorageMutationRequest, error) {
	return worker.sign(input, revision, fence)
}

func TestNativeSigningOccursAfterClaimAndRefusesLostOwnership(t *testing.T) {
	for change := 0; change < 9; change++ {
		t.Run(string(rune('a'+change)), func(t *testing.T) {
			control := newFakeStore()
			control.authoritative = true
			control.records["action:native"] = store.Record{Domain: "action", ID: "native", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}
			dispatched := false
			worker := &nativeSigningWorker{fakeWorkerClient: fakeWorkerClient{onExecute: func() { dispatched = true }, executeResp: &actionv1.StorageMutationResponse{Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, State: commonv1.EffectState_EFFECT_STATE_APPLIED, Fence: &commonv1.ActionFence{ActionId: "native", ExecutionEpoch: 1}, OperationId: "op", EffectId: "native:1", Etag: "\"etag\""}}}
			worker.sign = func(input *actionv1.StorageMutationRequest, revision, fence int64) (*actionv1.StorageMutationRequest, error) {
				if control.records["action:native"].State != "CLAIMED" || revision != 2 || fence != 10 {
					t.Fatal("signature requested before durable claim")
				}
				output := proto.Clone(input).(*actionv1.StorageMutationRequest)
				output.CanonicalAuthorization = []byte("native signature")
				switch change {
				case 1:
					return nil, context.DeadlineExceeded
				case 2:
					return nil, nil
				case 3:
					output.Fence.ExecutionEpoch = 2
				case 4:
					output.CanonicalAuthorization = nil
				case 5:
					output.ObjectKey = "other-object"
				case 6:
					control.authoritative = false
				case 7:
					control.lease.FencingToken++
				case 8:
					control.lease.LeaseUntil = 500
				}
				return output, nil
			}
			request := storageRequest("op")
			request.CanonicalAuthorization = nil
			_, err := NewCoordinator(control, worker, WithAuthoritative(true), WithNow(func() int64 { return 500 })).ExecuteStorageAction(context.Background(), "native", request)
			if change == 0 {
				if err != nil || !dispatched || control.records["action:native"].State != "SUCCEEDED" {
					t.Fatalf("native dispatch: %v", err)
				}
			} else if err == nil || dispatched {
				t.Fatalf("refusal dispatched change %d: %v", change, err)
			}
			if len(request.CanonicalAuthorization) != 0 {
				t.Fatal("caller request was mutated")
			}
		})
	}
}

func TestNativeSigningHasNoMissingCustodyFallback(t *testing.T) {
	control := newFakeStore()
	control.authoritative = true
	control.records["action:native"] = store.Record{Domain: "action", ID: "native", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}
	request := storageRequest("op")
	request.CanonicalAuthorization = nil
	_, err := NewCoordinator(control, &fakeWorkerClient{}, WithAuthoritative(true), WithNow(func() int64 { return 500 })).ExecuteStorageAction(context.Background(), "native", request)
	if !errors.Is(err, internalprotocol.ErrAuthenticationMissing) {
		t.Fatalf("missing custody did not refuse: %v", err)
	}
}

type signingLeaseStore struct {
	*store.Control
	authoritative bool
}

func (owner *signingLeaseStore) IsGoAuthoritative(string) (bool, error) {
	return owner.authoritative, nil
}

type signingLeasedWorker struct {
	leasedRPC
	sign func(context.Context, *actionv1.StorageMutationRequest) (*actionv1.StorageMutationRequest, error)
}

func (worker signingLeasedWorker) AuthorizeStorage(ctx context.Context, input *actionv1.StorageMutationRequest, _ int64, _ int64) (*actionv1.StorageMutationRequest, error) {
	return worker.sign(ctx, input)
}

func TestLeasedNativeSigningRefusesLateOrChangedAuthorization(t *testing.T) {
	for change := 0; change < 7; change++ {
		t.Run(string(rune('a'+change)), func(t *testing.T) {
			control, claim, clock := nativeClaim(t)
			owner := &signingLeaseStore{Control: control, authoritative: true} // isolated authority test double
			dispatched := false
			worker := signingLeasedWorker{leasedRPC: func(_ context.Context, input *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
				dispatched = true
				return claimedResponse(input), nil
			}, sign: func(ctx context.Context, input *actionv1.StorageMutationRequest) (*actionv1.StorageMutationRequest, error) {
				if err := ctx.Err(); err != nil {
					t.Fatal(err)
				}
				result := proto.Clone(input).(*actionv1.StorageMutationRequest)
				result.CanonicalAuthorization = []byte("native signature")
				switch change {
				case 1:
					return nil, context.DeadlineExceeded
				case 2:
					return nil, nil
				case 3:
					result.ObjectKey = "other"
				case 4:
					result.CanonicalAuthorization = nil
				case 5:
					clock.Store(2000)
				case 6:
					owner.authoritative = false
				}
				return result, nil
			}}
			request := storageRequest("leased-op")
			request.CanonicalAuthorization = nil
			_, err := NewCoordinator(owner, worker, WithAuthoritative(true), WithNow(clock.Load)).ExecuteClaimedStorageAction(context.Background(), claim, request)
			if change == 0 {
				if err != nil || !dispatched {
					t.Fatalf("signed leased execution: %v", err)
				}
			} else if err == nil || dispatched {
				t.Fatalf("leased refusal dispatched %d: %v", change, err)
			}
		})
	}
}
