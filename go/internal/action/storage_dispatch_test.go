package action

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

// These tests use the actual Go SQLite owner, but a worker double. They prove
// control durability and binding, not provider effects or process-kill recovery.
func storageRequest(operationID string) *actionv1.StorageMutationRequest {
	payload := []byte("qualification payload")
	digest := sha256.Sum256(payload)
	return &actionv1.StorageMutationRequest{
		OperationId: operationID, RequestId: "request", Nonce: "nonce", MutationType: "PUT_CHUNK", Provider: "s3",
		TargetIdentity: strings.Repeat("a", 64), Bucket: "qualification", Prefix: "prefix", ObjectKey: "object",
		PayloadDigest: hex.EncodeToString(digest[:]), ExpectedLength: uint64(len(payload)), Payload: payload,
		Precondition:           &actionv1.StoragePrecondition{ConditionType: actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_CREATE_ONLY},
		CanonicalAuthorization: []byte(`{"qualification":true}`), SchemaVersion: 1,
	}
}

func pendingStorageAction(t *testing.T, control *store.Control) {
	t.Helper()
	if err := control.Put(store.Record{Domain: "action", ID: "durable", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
}

type inspectingWorker struct {
	execute func(*actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error)
	query   func(*commonv1.ActionFence, string) (*actionv1.StorageMutationResponse, error)
}

func (w *inspectingWorker) ExecuteStorageMutation(_ context.Context, req *actionv1.StorageMutationRequest, _ string) (*actionv1.StorageMutationResponse, error) {
	return w.execute(req)
}

func (w *inspectingWorker) QueryStorageEffect(_ context.Context, fence *commonv1.ActionFence, operation string, _ string) (*actionv1.StorageMutationResponse, error) {
	return w.query(fence, operation)
}

func TestCoordinatorPersistsIntentBeforeRPCAndRecoversExactOperation(t *testing.T) {
	path := t.TempDir()
	options := store.OpenOptions{Path: path, Owner: "first", Now: func() int64 { return 500 }}
	control, err := store.OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	pendingStorageAction(t, control)
	request := storageRequest(" exact operation ")
	authDigest := sha256.Sum256(request.CanonicalAuthorization)
	want := store.StorageDispatchIntent{
		Schema: "storage-dispatch-intent-v1", ActionID: "durable", ExecutionEpoch: 1, OperationID: request.OperationId,
		RequestID: request.RequestId, Nonce: request.Nonce, MutationType: request.MutationType, Provider: request.Provider,
		TargetIdentity: request.TargetIdentity, Bucket: request.Bucket, Prefix: request.Prefix, ObjectKey: request.ObjectKey,
		PayloadDigest: request.PayloadDigest, ExpectedLength: request.ExpectedLength, Condition: "CREATE_ONLY",
		AuthorizationDigest: hex.EncodeToString(authDigest[:]), RequestSchemaVersion: request.SchemaVersion,
	}
	original := proto.Clone(request)
	worker := &inspectingWorker{execute: func(req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
		intent, exists, err := control.GetStorageDispatch("durable", 1)
		if err != nil || !exists || intent.Intent != want || intent.ClaimRevision != 3 {
			t.Errorf("RPC preceded exact durable intent: %+v exists=%v err=%v", intent, exists, err)
		}
		if req.Fence == nil || req.Fence.ActionId != "durable" || req.Fence.ExecutionEpoch != 1 {
			t.Error("wrong dispatch fence")
		}
		encoded, err := json.Marshal(intent)
		if err != nil || strings.Contains(string(encoded), string(request.Payload)) || strings.Contains(string(encoded), "qualification\\\":true") {
			t.Error("raw payload or authorization persisted")
		}
		return nil, context.DeadlineExceeded
	}}
	_, err = NewCoordinator(control, worker, WithNow(options.Now)).ExecuteStorageAction(context.Background(), "durable", request)
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("lost ACK became definite: %v", err)
	}
	if !proto.Equal(request, original) {
		t.Error("coordinator mutated caller request")
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	options.Owner = "successor"
	reopened, err := store.OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.Writer().FencingToken != 2 {
		t.Fatal("successor did not acquire next writer fence")
	}
	retained, exists, err := reopened.GetStorageDispatch("durable", 1)
	if err != nil || !exists || retained.Intent != want || retained.WriterFencingToken != 1 {
		t.Fatalf("successor rewrote immutable intent: %+v %v", retained, err)
	}
	queries := 0
	worker.query = func(fence *commonv1.ActionFence, operation string) (*actionv1.StorageMutationResponse, error) {
		queries++
		if operation != request.OperationId || fence.ActionId != "durable" || fence.ExecutionEpoch != 1 {
			t.Error("query lost durable binding")
		}
		return &actionv1.StorageMutationResponse{Fence: fence, OperationId: operation,
			Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED, State: commonv1.EffectState_EFFECT_STATE_APPLIED}, nil
	}
	coordinator := NewCoordinator(reopened, worker, WithNow(options.Now))
	if _, err := coordinator.ReconcileStorageAction(context.Background(), "durable", "substituted"); err == nil || queries != 0 {
		t.Fatalf("caller substituted persisted operation: queries=%d err=%v", queries, err)
	}
	if _, err := coordinator.ReconcileStorageAction(context.Background(), "durable", ""); err != nil || queries != 1 {
		t.Fatalf("recovery must derive exact operation from Go journal: queries=%d err=%v", queries, err)
	}
	record, _, err := reopened.Get("action", "durable")
	if err != nil || record.State != "SUCCEEDED" {
		t.Fatalf("recovery settlement: %+v %v", record, err)
	}
}

func TestCoordinatorCannotAdoptLegacyExecutingAction(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "legacy", Now: func() int64 { return 500 }})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	pendingStorageAction(t, control)
	for index, state := range []string{"CLAIMED", "EXECUTING"} {
		if err := control.Put(store.Record{Domain: "action", ID: "durable", Revision: int64(index + 2), ExecutionEpoch: 1, State: state}); err != nil {
			t.Fatal(err)
		}
	}
	worker := &fakeWorkerClient{onQuery: func() { t.Error("legacy action was adopted for a query") }}
	if _, err := NewCoordinator(control, worker, WithNow(func() int64 { return 500 })).ReconcileStorageAction(context.Background(), "durable", "invented"); err == nil {
		t.Fatal("legacy association was inferred")
	}
	record, _, err := control.Get("action", "durable")
	if err != nil || record.State != "EXECUTING" || record.Revision != 3 {
		t.Fatalf("legacy record changed: %+v %v", record, err)
	}
}
