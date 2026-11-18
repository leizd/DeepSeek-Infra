package worker

import (
	"context"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
)

// ResolveTargetMarker selects the sole effect RPC only for a never-dispatched
// claim. A disconnected RPC is never permission to send it a second time.
func (client *Client) ResolveTargetMarker(ctx context.Context, operation store.TargetMarkerOperation, writer uint64) (*actionv1.TargetMarkerReceipt, error) {
	if client == nil || !client.tlsSecured || !client.bearerAttached || client.targetMarker == nil {
		return nil, ErrWorkerTLSRequired
	}
	if ctx == nil || operation.Claim == nil || operation.Claim.Fence == nil || len(operation.ClaimSHA256) != 32 || writer == 0 {
		return nil, store.ErrTargetMarkerClaim
	}
	bounded, cancel := context.WithTimeout(ctx, targetObservationTimeout)
	defer cancel()
	outgoing, err := client.outgoingContext(bounded, "")
	if err != nil {
		return nil, err
	}
	option := grpc.MaxCallRecvMsgSize(store.MaxTargetMarkerReceiptBytes)
	switch operation.State {
	case "CLAIMED":
		return client.targetMarker.Initialize(outgoing, operation.Claim, option)
	case "DISPATCHED":
		return client.targetMarker.Reconcile(outgoing, &actionv1.ReconcileTargetMarkerRequest{
			SchemaVersion: 1, Fence: operation.Claim.Fence, OperationId: operation.Claim.OperationId,
			CurrentWriterFencingToken: writer, ClaimSha256: operation.ClaimSHA256,
		}, option)
	default:
		return nil, store.ErrTargetMarkerClaim
	}
}
