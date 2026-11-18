package worker

import (
	"bytes"
	"context"
	"errors"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type targetMarkerRPCFixture struct {
	actionv1.BackupTargetMarkerWorkerClient
	admitted  []*actionv1.TargetMarkerClaim
	recovered []*actionv1.ReconcileTargetMarkerRequest
}

func (rpc *targetMarkerRPCFixture) Initialize(_ context.Context, input *actionv1.TargetMarkerClaim, _ ...grpc.CallOption) (*actionv1.TargetMarkerReceipt, error) {
	rpc.admitted = append(rpc.admitted, proto.Clone(input).(*actionv1.TargetMarkerClaim))
	return nil, status.Error(codes.Unavailable, "isolated worker reply was lost")
}

func (rpc *targetMarkerRPCFixture) Reconcile(_ context.Context, input *actionv1.ReconcileTargetMarkerRequest, _ ...grpc.CallOption) (*actionv1.TargetMarkerReceipt, error) {
	rpc.recovered = append(rpc.recovered, proto.Clone(input).(*actionv1.ReconcileTargetMarkerRequest))
	return &actionv1.TargetMarkerReceipt{Effect: &actionv1.TargetMarkerEffect{Fence: input.Fence}}, nil
}

func TestTargetInspectionClientMarkerReconcilesOriginalFenceAfterLostReply(t *testing.T) {
	rpc := &targetMarkerRPCFixture{}
	client := &Client{tlsSecured: true, bearerAttached: true, targetMarker: rpc}
	op := store.TargetMarkerOperation{Claim: &actionv1.TargetMarkerClaim{
		OperationId:        store.PublicTargetOperationPrefix + "worker-fenced-recovery",
		WriterFencingToken: 7,
	}, ClaimSHA256: bytes.Repeat([]byte{1}, 32), State: "CLAIMED"}
	op.Claim.Fence = &commonv1.ActionFence{ActionId: op.Claim.OperationId, ExecutionEpoch: 3}
	ctx := context.Background()
	for _, invalid := range []*Client{nil, {}, {tlsSecured: true, bearerAttached: true}, {targetMarker: rpc, bearerAttached: true}} {
		if _, err := invalid.ResolveTargetMarker(ctx, op, 7); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatal("client attempted marker RPC without its pinned TLS transport", err)
		}
	}
	unbound := op
	unbound.ClaimSHA256 = nil
	if _, err := client.ResolveTargetMarker(ctx, unbound, 7); !errors.Is(err, store.ErrTargetMarkerClaim) {
		t.Fatal("client transmitted unbound operation", err)
	}
	duplicate := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer isolated-test-credential")
	if _, err := client.ResolveTargetMarker(duplicate, op, 7); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatal("client accepted a second transport credential", err)
	}
	if len(rpc.admitted)+len(rpc.recovered) != 0 {
		t.Fatal("denied inputs reached the RPC")
	}
	if _, err := client.ResolveTargetMarker(ctx, op, 7); status.Code(err) != codes.Unavailable || len(rpc.admitted) != 1 ||
		!proto.Equal(rpc.admitted[0], op.Claim) || len(rpc.recovered) != 0 {
		t.Fatal("lost reply was hidden or retried", err)
	}
	op.State = "DISPATCHED"
	witness, err := client.ResolveTargetMarker(ctx, op, 8)
	if err != nil || witness == nil || len(rpc.recovered) != 1 || len(rpc.admitted) != 1 {
		t.Fatal("recovery did not use read-only RPC", err)
	}
	query := rpc.recovered[0]
	if query.SchemaVersion != 1 || query.CurrentWriterFencingToken != 8 || query.OperationId != op.Claim.OperationId ||
		!proto.Equal(query.Fence, op.Claim.Fence) || !bytes.Equal(query.ClaimSha256, op.ClaimSHA256) {
		t.Fatal("read-only RPC rebound the original dispatch")
	}
	op.State = "SETTLED"
	if _, err := client.ResolveTargetMarker(ctx, op, 8); !errors.Is(err, store.ErrTargetMarkerClaim) || len(rpc.admitted) != 1 || len(rpc.recovered) != 1 {
		t.Fatal("completed operation caused another worker call", err)
	}
}
