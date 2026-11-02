package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"io"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

const mediaPatchResultChunkBytes = 256 * 1024

type mediaPatchResultReceiver interface {
	Recv() (*mediav1.MediaPatchResultChunk, error)
}

// The caller must first settle the signed receipt through the authoritative
// control store. This is a read of that original operation, never a retry.
func (c *Client) ReadMediaPatchResult(ctx context.Context, outcome *mediav1.MediaPatchOutcome) (*mediav1.MediaObject, error) {
	if c == nil || !c.tlsSecured || !c.bearerAttached || c.media == nil {
		return nil, ErrWorkerTLSRequired
	}
	if outcome == nil || outcome.SchemaVersion != 2 || outcome.Patched == nil || outcome.OriginalFence == nil ||
		outcome.State != mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED {
		return nil, ErrInvalidWorkerResponse
	}
	ctx, err := c.outgoingContext(ctx, "")
	if err != nil {
		return nil, err
	}
	stream, err := c.media.ReadPatchResult(ctx, &mediav1.ReadMediaPatchResultRequest{
		SchemaVersion: 1, OperationId: outcome.OperationId, OriginalFence: outcome.OriginalFence,
	}, grpc.MaxCallRecvMsgSize(mediaPatchResultChunkBytes+1024))
	if err != nil {
		return nil, err
	}
	return receiveMediaPatchResult(stream, outcome.Patched)
}

func receiveMediaPatchResult(stream mediaPatchResultReceiver, receipt *mediav1.MediaPatchedResult) (*mediav1.MediaObject, error) {
	if stream == nil || receipt == nil || receipt.RecordBytes == 0 || len(receipt.RecordSha256) != 32 ||
		receipt.RecordBytes > uint64(int(^uint(0)>>1)) {
		return nil, ErrInvalidWorkerResponse
	}
	first, err := stream.Recv()
	if err != nil {
		return nil, err
	}
	if first == nil || first.SchemaVersion != 1 || first.Offset != 0 || len(first.RecordChunk) != 0 ||
		len(first.ProtoReflect().GetUnknown()) != 0 || !proto.Equal(first.Manifest, receipt) {
		return nil, ErrInvalidWorkerResponse
	}
	var encoded []byte
	digest := sha256.New()
	var offset uint64
	for {
		chunk, err := stream.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		if chunk == nil || chunk.SchemaVersion != 1 || chunk.Manifest != nil || chunk.Offset != offset ||
			len(chunk.ProtoReflect().GetUnknown()) != 0 || len(chunk.RecordChunk) == 0 ||
			len(chunk.RecordChunk) > mediaPatchResultChunkBytes || uint64(len(chunk.RecordChunk)) > receipt.RecordBytes-offset {
			return nil, ErrInvalidWorkerResponse
		}
		encoded = append(encoded, chunk.RecordChunk...)
		_, _ = digest.Write(chunk.RecordChunk)
		offset += uint64(len(chunk.RecordChunk))
	}
	if offset != receipt.RecordBytes || !bytes.Equal(digest.Sum(nil), receipt.RecordSha256) {
		return nil, ErrInvalidWorkerResponse
	}
	record := &mediav1.MediaObject{}
	if proto.Unmarshal(encoded, record) != nil {
		return nil, ErrInvalidWorkerResponse
	}
	canonical, err := (proto.MarshalOptions{Deterministic: true}).Marshal(record)
	if err != nil || !bytes.Equal(canonical, encoded) {
		return nil, ErrInvalidWorkerResponse
	}
	if _, err := mediamodel.ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: record}}); err != nil {
		return nil, ErrInvalidWorkerResponse
	}
	return record, nil
}
