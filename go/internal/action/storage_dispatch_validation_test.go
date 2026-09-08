package action

import (
	"context"
	"errors"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

func TestCoordinatorRejectsUnrepresentableIntentBeforeChangingAction(t *testing.T) {
	for name, corrupt := range map[string]func(*actionv1.StorageMutationRequest){
		"unknown request fields": func(r *actionv1.StorageMutationRequest) { r.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01}) },
		"unknown fence fields": func(r *actionv1.StorageMutationRequest) {
			r.Fence = &commonv1.ActionFence{ActionId: "durable", ExecutionEpoch: 1}
			r.Fence.ProtoReflect().SetUnknown([]byte{0x18, 1})
		},
		"unknown precondition fields": func(r *actionv1.StorageMutationRequest) { r.Precondition.ProtoReflect().SetUnknown([]byte{0x18, 1}) },
		"missing precondition":        func(r *actionv1.StorageMutationRequest) { r.Precondition = nil },
		"unknown condition":           func(r *actionv1.StorageMutationRequest) { r.Precondition.ConditionType = 99 },
		"short payload":               func(r *actionv1.StorageMutationRequest) { r.ExpectedLength++ },
		"oversize payload": func(r *actionv1.StorageMutationRequest) {
			r.Payload = make([]byte, 8*1024*1024+1)
			r.ExpectedLength = uint64(len(r.Payload))
		},
		"oversize authority": func(r *actionv1.StorageMutationRequest) { r.CanonicalAuthorization = make([]byte, 16*1024+1) },
		"blank operation":    func(r *actionv1.StorageMutationRequest) { r.OperationId = " " },
		"invalid metadata":   func(r *actionv1.StorageMutationRequest) { r.Provider = "filesystem" },
	} {
		t.Run(name, func(t *testing.T) {
			control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "control", Now: func() int64 { return 500 }})
			if err != nil {
				t.Fatal(err)
			}
			defer control.Close()
			pendingStorageAction(t, control)
			req := storageRequest("operation")
			corrupt(req)
			worker := &fakeWorkerClient{onExecute: func() { t.Error("invalid request reached worker") }}
			if _, err := NewCoordinator(control, worker, WithNow(func() int64 { return 500 })).ExecuteStorageAction(context.Background(), "durable", req); !errors.Is(err, store.ErrInvalidStorageIntent) {
				t.Fatalf("invalid request: %v", err)
			}
			record, _, err := control.Get("action", "durable")
			if err != nil || record.State != "PENDING" || record.Revision != 1 {
				t.Fatalf("invalid request changed action: %+v %v", record, err)
			}
			if _, exists, err := control.GetStorageDispatch("durable", 1); err != nil || exists {
				t.Fatalf("invalid request claimed intent: %v %v", exists, err)
			}
		})
	}
}

func TestCoordinatorRPCArgumentMutationCannotRebindResult(t *testing.T) {
	for _, query := range []bool{false, true} {
		control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "control", Now: func() int64 { return 500 }})
		if err != nil {
			t.Fatal(err)
		}
		defer control.Close()
		pendingStorageAction(t, control)
		request := storageRequest("operation")
		request.Precondition = &actionv1.StoragePrecondition{ConditionType: actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_IF_MATCH, ExpectedEtag: "\"opaque\""}
		original := proto.Clone(request)
		response := func(fence *commonv1.ActionFence, operation string) *actionv1.StorageMutationResponse {
			return &actionv1.StorageMutationResponse{Fence: fence, OperationId: operation, Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, State: commonv1.EffectState_EFFECT_STATE_APPLIED}
		}
		worker := &inspectingWorker{execute: func(req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
			if query {
				return nil, context.DeadlineExceeded
			}
			req.Fence.ExecutionEpoch = 42
			req.OperationId = "substituted"
			req.Payload[0] = 'x'
			req.CanonicalAuthorization[0] = 'x'
			req.Precondition.ExpectedEtag = "\"different\""
			return response(req.Fence, req.OperationId), nil
		}, query: func(fence *commonv1.ActionFence, operation string) (*actionv1.StorageMutationResponse, error) {
			fence.ExecutionEpoch = 42
			return response(fence, operation), nil
		}}
		coordinator := NewCoordinator(control, worker, WithNow(func() int64 { return 500 }))
		if _, err := coordinator.ExecuteStorageAction(context.Background(), "durable", request); !errors.Is(err, ErrStorageMutationUncertain) {
			t.Fatal(err)
		}
		if query {
			if _, err := coordinator.ReconcileStorageAction(context.Background(), "durable", ""); !errors.Is(err, ErrStorageMutationUncertain) {
				t.Fatal(err)
			}
		}
		if !proto.Equal(original, request) {
			t.Fatal("worker mutated caller-owned request")
		}
		intent, exists, err := control.GetStorageDispatch("durable", 1)
		if err != nil || !exists || intent.Intent.Condition != "IF_MATCH" || intent.Intent.ExpectedETag != "\"opaque\"" || intent.Intent.OperationID != "operation" {
			t.Fatalf("intent changed: %+v %v", intent, err)
		}
		record, _, err := control.Get("action", "durable")
		if err != nil || record.State != "EFFECT_UNKNOWN" {
			t.Fatalf("mutated argument settled action: %+v %v", record, err)
		}
	}
}

type dispatchReadFailure struct {
	ControlStore
	err error
}

func (s dispatchReadFailure) GetStorageDispatch(string, uint64) (store.StorageDispatch, bool, error) {
	return store.StorageDispatch{}, false, s.err
}

func TestCoordinatorPropagatesDurableIntentReadFailure(t *testing.T) {
	control := newFakeStore()
	control.records["action:a"] = store.Record{Domain: "action", ID: "a", ExecutionEpoch: 1, State: "EXECUTING"}
	sentinel := errors.New("intent journal unavailable")
	worker := &fakeWorkerClient{onQuery: func() { t.Error("unreadable intent reached worker") }}
	_, err := NewCoordinator(dispatchReadFailure{control, sentinel}, worker, WithNow(func() int64 { return 500 })).ReconcileStorageAction(context.Background(), "a", "op")
	if !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
}
