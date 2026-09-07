package worker

import (
	"context"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestStorageResultsRequireExactIdentity(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "bound-action", ExecutionEpoch: 9}
	for name, mutate := range map[string]func(*actionv1.StorageMutationResponse){
		"missing fence":     func(r *actionv1.StorageMutationResponse) { r.Fence = nil },
		"other action":      func(r *actionv1.StorageMutationResponse) { r.Fence.ActionId = "other" },
		"other epoch":       func(r *actionv1.StorageMutationResponse) { r.Fence.ExecutionEpoch++ },
		"missing operation": func(r *actionv1.StorageMutationResponse) { r.OperationId = "" },
		"other operation":   func(r *actionv1.StorageMutationResponse) { r.OperationId = "other" },
	} {
		for _, query := range []bool{false, true} {
			t.Run(name+map[bool]string{false: "/execute", true: "/query"}[query], func(t *testing.T) {
				response := &actionv1.StorageMutationResponse{
					Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
					State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
					Fence:       &commonv1.ActionFence{ActionId: fence.ActionId, ExecutionEpoch: fence.ExecutionEpoch},
					OperationId: "bound-operation", EffectId: "bound-action:9", Etag: "\"etag\"",
				}
				mutate(response)
				client := New(&fakeWorkerRPC{storageResponse: response, storageQueryResp: response})
				var got *actionv1.StorageMutationResponse
				var err error
				if query {
					got, err = client.QueryStorageEffect(context.Background(), fence, "bound-operation", "")
				} else {
					got, err = client.ExecuteStorageMutation(context.Background(), &actionv1.StorageMutationRequest{Fence: fence, OperationId: "bound-operation"}, "")
				}
				if err != ErrInvalidWorkerResponse || got != nil {
					t.Fatalf("unbound result escaped validation: result=%+v error=%v", got, err)
				}
			})
		}
	}
}

func TestStorageQueryRejectsEmptyOperationBeforeRPC(t *testing.T) {
	rpc := &fakeWorkerRPC{}
	_, err := New(rpc).QueryStorageEffect(context.Background(), &commonv1.ActionFence{ActionId: "a", ExecutionEpoch: 1}, "", "")
	if err != store.ErrAuthorityRequestOperationInvalid || rpc.storageQueryReq != nil {
		t.Fatalf("empty operation reached worker: %v %+v", err, rpc.storageQueryReq)
	}
}

func TestStorageQueryRecordedNoEffectStates(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "a", ExecutionEpoch: 1}
	for _, code := range []string{"Rejected", "Failed"} {
		status := actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED
		if code == "Failed" {
			status = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED
		}
		for _, recorded := range []bool{false, true} {
			response := &actionv1.StorageMutationResponse{Status: status, State: commonv1.EffectState_EFFECT_STATE_UNKNOWN,
				Fence: fence, OperationId: "op", Error: &commonv1.ErrorDetail{Code: code}}
			if recorded {
				response.State = commonv1.EffectState_EFFECT_STATE_NOT_APPLIED
			}
			_, err := New(&fakeWorkerRPC{storageQueryResp: response}).QueryStorageEffect(context.Background(), fence, "op", "")
			if recorded && err != nil {
				t.Fatalf("recorded %s result rejected: %v", code, err)
			}
			if !recorded && err != ErrInvalidWorkerResponse {
				t.Fatalf("unproven %s result trusted: %v", code, err)
			}
		}
	}
}
