package worker

import (
	"context"
	"errors"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
	"testing"
)

// This fixture tests forwarding and credential-conflict refusal only. It is not
// a TLS server or Rust worker and cannot qualify transport/custody/file effects.
type mediaForwardingFixture struct {
	create                       *mediav1.ExecuteMediaPatchRequest
	createOptions                int
	execute                      *mediav1.ExecuteMediaPatchRequest
	delete                       *mediav1.ExecuteMediaDeleteRequest
	query                        *mediav1.QueryMediaPatchRequest
	executeOptions, queryOptions int
	deleteOptions                int
	calls                        int
	failure                      error
}

func (f *mediaForwardingFixture) ExecuteCreate(_ context.Context, r *mediav1.ExecuteMediaPatchRequest, opts ...grpc.CallOption) (*mediav1.MediaPatchOutcome, error) {
	f.calls++
	f.create = proto.Clone(r).(*mediav1.ExecuteMediaPatchRequest)
	f.createOptions = len(opts)
	return &mediav1.MediaPatchOutcome{OperationId: r.OperationId}, f.failure
}

func (f *mediaForwardingFixture) ExecuteDelete(_ context.Context, r *mediav1.ExecuteMediaDeleteRequest, opts ...grpc.CallOption) (*mediav1.MediaPatchOutcome, error) {
	f.calls++
	f.delete = proto.Clone(r).(*mediav1.ExecuteMediaDeleteRequest)
	f.deleteOptions = len(opts)
	return &mediav1.MediaPatchOutcome{OperationId: r.OperationId}, f.failure
}

func (f *mediaForwardingFixture) ExecutePatch(_ context.Context, r *mediav1.ExecuteMediaPatchRequest, opts ...grpc.CallOption) (*mediav1.MediaPatchOutcome, error) {
	f.calls++
	f.execute = proto.Clone(r).(*mediav1.ExecuteMediaPatchRequest)
	f.executeOptions = len(opts)
	return &mediav1.MediaPatchOutcome{OperationId: r.OperationId}, f.failure
}
func (f *mediaForwardingFixture) QueryPatch(_ context.Context, r *mediav1.QueryMediaPatchRequest, opts ...grpc.CallOption) (*mediav1.QueryMediaPatchResponse, error) {
	f.calls++
	f.query = proto.Clone(r).(*mediav1.QueryMediaPatchRequest)
	f.queryOptions = len(opts)
	return &mediav1.QueryMediaPatchResponse{Found: true}, f.failure
}
func TestMediaForwardingPreservesTheOriginalOperationAndTransportErrors(t *testing.T) {
	f := &mediaForwardingFixture{}
	c := &Client{tlsSecured: true, bearerAttached: true, media: f}
	fence := &commonv1.ActionFence{ActionId: "original-operation", ExecutionEpoch: 7}
	execute := &mediav1.ExecuteMediaPatchRequest{SchemaVersion: 1, OperationId: fence.ActionId, Fence: fence}
	query := &mediav1.QueryMediaPatchRequest{SchemaVersion: 1, OperationId: fence.ActionId, OriginalFence: fence}
	deletion := &mediav1.ExecuteMediaDeleteRequest{SchemaVersion: 1, OperationId: fence.ActionId, Fence: fence}
	result, err := c.ExecuteMediaPatch(context.Background(), execute)
	if err != nil || result.OperationId != fence.ActionId || !proto.Equal(f.execute, execute) || f.executeOptions != 2 {
		t.Fatalf("execution binding lost: %#v %v", result, err)
	}
	response, err := c.QueryMediaPatch(context.Background(), query)
	if err != nil || !response.Found || !proto.Equal(f.query, query) || f.queryOptions != 1 {
		t.Fatal("original query/options changed", err)
	}
	deleted, err := c.ExecuteMediaDelete(context.Background(), deletion)
	if err != nil || deleted.OperationId != fence.ActionId || !proto.Equal(f.delete, deletion) || f.deleteOptions != 2 {
		t.Fatal("delete operation or transport bound changed", err)
	}
	f.failure = errors.New("isolated transport failure")
	if _, err := c.ExecuteMediaPatch(context.Background(), execute); !errors.Is(err, f.failure) {
		t.Fatal("execution failure hidden", err)
	}
	if _, err := c.QueryMediaPatch(context.Background(), query); !errors.Is(err, f.failure) {
		t.Fatal("query failure hidden", err)
	}
	if _, err := c.ExecuteMediaDelete(context.Background(), deletion); !errors.Is(err, f.failure) {
		t.Fatal("deletion failure hidden", err)
	}
	if f.calls != 6 {
		t.Fatal("transport was retried", f.calls)
	}
}
func TestMediaForwardingRejectsConflictingCredentialsBeforeCallingThePeer(t *testing.T) {
	f := &mediaForwardingFixture{}
	c := &Client{tlsSecured: true, bearerAttached: true, media: f}
	for _, ctx := range []context.Context{nil, metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer untrusted"))} {
		if _, err := c.ExecuteMediaPatch(ctx, &mediav1.ExecuteMediaPatchRequest{}); err == nil {
			t.Fatal("invalid execution context accepted")
		}
		if _, err := c.QueryMediaPatch(ctx, &mediav1.QueryMediaPatchRequest{}); err == nil {
			t.Fatal("invalid recovery context accepted")
		}
		if _, err := c.ExecuteMediaDelete(ctx, &mediav1.ExecuteMediaDeleteRequest{}); err == nil {
			t.Fatal("invalid deletion context accepted")
		}
	}
	if f.calls != 0 {
		t.Fatal("unauthorized request reached peer")
	}
}

func TestMediaCreateForwarding(t *testing.T) {
	fixture := &mediaForwardingFixture{}
	client := &Client{tlsSecured: true, bearerAttached: true, media: fixture}
	fence := &commonv1.ActionFence{ActionId: "original-create", ExecutionEpoch: 7}
	request := &mediav1.ExecuteMediaPatchRequest{SchemaVersion: 1, OperationId: fence.ActionId, Fence: fence}
	result, err := client.ExecuteMediaCreate(context.Background(), request)
	if err != nil || result == nil || result.OperationId != fence.ActionId || !proto.Equal(fixture.create, request) || fixture.createOptions != 1 || fixture.execute != nil || fixture.delete != nil {
		t.Fatal("CREATE forwarding lost its original identity or invoked another mutation", err)
	}
	fixture.failure = errors.New("isolated creation transport failure")
	if _, err = client.ExecuteMediaCreate(context.Background(), request); !errors.Is(err, fixture.failure) {
		t.Fatal("creation failure hidden", err)
	}
	for _, ctx := range []context.Context{nil, metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer untrusted"))} {
		if _, err = client.ExecuteMediaCreate(ctx, request); err == nil {
			t.Fatal("untrusted creation context accepted")
		}
	}
	if fixture.calls != 2 {
		t.Fatal("creation retried or unauthorized request reached peer", fixture.calls)
	}
}
