package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

func (c *Client) StageMediaCreate(ctx context.Context, claim *mediav1.MediaPatchClaim, payload []byte) error {
	if c == nil || !c.tlsSecured || !c.bearerAttached || c.mediaCreation == nil {
		return ErrWorkerTLSRequired
	}
	if claim == nil || claim.Intent == nil || claim.Fence == nil || claim.Intent.Create == nil {
		return ErrInvalidWorkerResponse
	}
	manifest := claim.Intent.Create
	digest := sha256.Sum256(payload)
	if uint64(len(payload)) != manifest.PayloadBytes || !bytes.Equal(digest[:], manifest.PayloadSha256) || len(payload) > mediamodel.MaxCreatePayloadBytes {
		return ErrInvalidWorkerResponse
	}
	ctx, err := c.outgoingContext(ctx, "")
	if err != nil {
		return err
	}
	stream, err := c.mediaCreation.Stage(ctx, grpc.MaxCallSendMsgSize(mediamodel.CreateChunkBytes+1024))
	if err != nil {
		return err
	}
	message := &mediav1.MediaCreateStageRequest{SchemaVersion: 1, OperationId: claim.Intent.OperationId, Fence: claim.Fence,
		Part: &mediav1.MediaCreateStageRequest_Manifest{Manifest: manifest}}
	if err = stream.Send(message); err != nil {
		return err
	}
	for offset := 0; offset < len(payload); offset += mediamodel.CreateChunkBytes {
		end := offset + mediamodel.CreateChunkBytes
		if end > len(payload) {
			end = len(payload)
		}
		// SendMsg may retain the message for lazy tracing/stats observers.
		// Keep every sent message immutable until the stream has completed.
		message := &mediav1.MediaCreateStageRequest{SchemaVersion: 1, OperationId: claim.Intent.OperationId, Fence: claim.Fence,
			Part: &mediav1.MediaCreateStageRequest_Chunk{Chunk: payload[offset:end]}}
		if err = stream.Send(message); err != nil {
			return err
		}
	}
	response, err := stream.CloseAndRecv()
	if err != nil {
		return err
	}
	if response == nil || !proto.Equal(response.Manifest, manifest) {
		return ErrInvalidWorkerResponse
	}
	return nil
}

func (c *Client) ExecuteMediaCreate(ctx context.Context, request *mediav1.ExecuteMediaPatchRequest) (*mediav1.MediaPatchOutcome, error) {
	if c == nil || !c.tlsSecured || !c.bearerAttached || c.media == nil {
		return nil, ErrWorkerTLSRequired
	}
	ctx, err := c.outgoingContext(ctx, "")
	if err != nil {
		return nil, err
	}
	return c.media.ExecuteCreate(ctx, request, grpc.MaxCallRecvMsgSize(mediamodel.MaxMessageBytes))
}

func (c *Client) ReadMediaCreateResult(ctx context.Context, outcome *mediav1.MediaPatchOutcome) (*mediav1.MediaObject, error) {
	if c == nil || !c.tlsSecured || !c.bearerAttached || c.mediaCreation == nil {
		return nil, ErrWorkerTLSRequired
	}
	if outcome == nil || outcome.Created == nil || outcome.OriginalFence == nil {
		return nil, ErrInvalidWorkerResponse
	}
	ctx, err := c.outgoingContext(ctx, "")
	if err != nil {
		return nil, err
	}
	response, err := c.mediaCreation.ReadResult(ctx, &mediav1.ReadMediaCreateResultRequest{SchemaVersion: 1,
		OperationId: outcome.OperationId, OriginalFence: outcome.OriginalFence}, grpc.MaxCallRecvMsgSize(mediamodel.MaxCreatePayloadBytes+1024))
	if err != nil {
		return nil, err
	}
	if response == nil || response.Media == nil {
		return nil, ErrInvalidWorkerResponse
	}
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(response.Media)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(encoded)
	if uint64(len(encoded)) != outcome.Created.RecordBytes || !bytes.Equal(digest[:], outcome.Created.RecordSha256) {
		return nil, ErrInvalidWorkerResponse
	}
	return response.Media, nil
}
