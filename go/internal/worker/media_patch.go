package worker

import (
	"context"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc"
)

// Media mutations require the production TLS/pinned-service channel. Plaintext
// shadow clients cannot obtain this capability by supplying an operation id.
func (c *Client) ExecuteMediaPatch(ctx context.Context, r *mediav1.ExecuteMediaPatchRequest) (*mediav1.MediaPatchOutcome, error) {
	if c == nil || !c.tlsSecured || !c.bearerAttached || c.media == nil {
		return nil, ErrWorkerTLSRequired
	}
	ctx, err := c.outgoingContext(ctx, "")
	if err != nil {
		return nil, err
	}
	return c.media.ExecutePatch(ctx, r, grpc.MaxCallRecvMsgSize(4*1024*1024), grpc.MaxCallSendMsgSize(4*1024*1024))
}
func (c *Client) QueryMediaPatch(ctx context.Context, r *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error) {
	if c == nil || !c.tlsSecured || !c.bearerAttached || c.media == nil {
		return nil, ErrWorkerTLSRequired
	}
	ctx, err := c.outgoingContext(ctx, "")
	if err != nil {
		return nil, err
	}
	return c.media.QueryPatch(ctx, r, grpc.MaxCallRecvMsgSize(4*1024*1024))
}
