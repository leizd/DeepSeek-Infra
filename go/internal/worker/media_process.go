package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"strings"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

func (c *Client) ExecuteMediaProcess(ctx context.Context, request *mediav1.ExecuteMediaProcessRequest) (*mediav1.MediaPatchOutcome, error) {
	if c == nil || !c.tlsSecured || !c.bearerAttached || c.media == nil {
		return nil, ErrWorkerTLSRequired
	}
	ctx, err := c.outgoingContext(ctx, "")
	if err != nil {
		return nil, err
	}
	return c.media.ExecuteProcess(ctx, request, grpc.MaxCallRecvMsgSize(mediamodel.MaxMessageBytes))
}

// The record, segments and index count belong to this exact retained operation.
// A current library GET cannot replace the immutable, signed result read.
func (c *Client) ReadMediaProcessResult(ctx context.Context, outcome *mediav1.MediaPatchOutcome) (*mediav1.ReadMediaCreateResultResponse, error) {
	if c == nil || !c.tlsSecured || !c.bearerAttached || c.mediaCreation == nil {
		return nil, ErrWorkerTLSRequired
	}
	if outcome == nil || outcome.Processed == nil || outcome.Created != nil || outcome.Media != nil || outcome.OriginalFence == nil ||
		!strings.HasPrefix(outcome.OperationId, "media-process-") || outcome.OperationId != outcome.OriginalFence.ActionId ||
		!proto.Equal(outcome.Processed.OriginalFence, outcome.OriginalFence) {
		return nil, ErrInvalidWorkerResponse
	}
	p := outcome.Processed
	if p.RecordBytes == 0 || p.RecordBytes > mediamodel.MaxCreatePayloadBytes || p.SegmentsBytes > mediamodel.MaxCreatePayloadBytes || len(p.RecordSha256) != 32 || len(p.SegmentsSha256) != 32 {
		return nil, ErrInvalidWorkerResponse
	}
	ctx, err := c.outgoingContext(ctx, "")
	if err != nil {
		return nil, err
	}
	response, err := c.mediaCreation.ReadResult(ctx, &mediav1.ReadMediaCreateResultRequest{SchemaVersion: 1, OperationId: outcome.OperationId, OriginalFence: outcome.OriginalFence}, grpc.MaxCallRecvMsgSize(2*mediamodel.MaxCreatePayloadBytes+1024))
	if err != nil {
		return nil, err
	}
	if response == nil || response.Media == nil || response.Segments == nil || response.Indexed != p.Indexed || len(response.ProtoReflect().GetUnknown()) != 0 {
		return nil, ErrInvalidWorkerResponse
	}
	for _, item := range []struct {
		message proto.Message
		size    uint64
		digest  []byte
	}{
		{response.Media, p.RecordBytes, p.RecordSha256}, {response.Segments, p.SegmentsBytes, p.SegmentsSha256},
	} {
		encoded, e := (proto.MarshalOptions{Deterministic: true}).Marshal(item.message)
		if e != nil {
			return nil, e
		}
		digest := sha256.Sum256(encoded)
		if uint64(len(encoded)) != item.size || !bytes.Equal(digest[:], item.digest) {
			return nil, ErrInvalidWorkerResponse
		}
	}
	if _, err = mediamodel.ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: response.Media}}); err != nil {
		return nil, ErrInvalidWorkerResponse
	}
	if _, err = mediamodel.ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ArrayValue{ArrayValue: response.Segments}}); err != nil {
		return nil, ErrInvalidWorkerResponse
	}
	return response, nil
}
