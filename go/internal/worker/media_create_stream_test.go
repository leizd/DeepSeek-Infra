package worker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

// These deterministic client-boundary fixtures complement the real TLS test.
// They do not qualify Rust custody or an actual network exchange.
type mediaCreateStreamFixture struct {
	grpc.ClientStream
	sent         []*mediav1.MediaCreateStageRequest
	failAt       int
	failure      error
	closeFailure error
	response     *mediav1.MediaCreateStageResponse
}

func (s *mediaCreateStreamFixture) Send(r *mediav1.MediaCreateStageRequest) error {
	if s.failAt == len(s.sent)+1 {
		return s.failure
	}
	s.sent = append(s.sent, proto.Clone(r).(*mediav1.MediaCreateStageRequest))
	return nil
}
func (s *mediaCreateStreamFixture) CloseAndRecv() (*mediav1.MediaCreateStageResponse, error) {
	return s.response, s.closeFailure
}

type mediaCreateClientFixture struct {
	stream                                  *mediaCreateStreamFixture
	openFailure, readFailure                error
	response                                *mediav1.ReadMediaCreateResultResponse
	opened, read, stageOptions, readOptions int
	request                                 *mediav1.ReadMediaCreateResultRequest
}

func (f *mediaCreateClientFixture) Stage(_ context.Context, opts ...grpc.CallOption) (grpc.ClientStreamingClient[mediav1.MediaCreateStageRequest, mediav1.MediaCreateStageResponse], error) {
	f.opened++
	f.stageOptions = len(opts)
	return f.stream, f.openFailure
}
func (f *mediaCreateClientFixture) ReadResult(_ context.Context, r *mediav1.ReadMediaCreateResultRequest, opts ...grpc.CallOption) (*mediav1.ReadMediaCreateResultResponse, error) {
	f.read++
	f.readOptions = len(opts)
	f.request = proto.Clone(r).(*mediav1.ReadMediaCreateResultRequest)
	return f.response, f.readFailure
}

func TestMediaCreateClientBoundedMessages(t *testing.T) {
	payload := bytes.Repeat([]byte{7}, mediamodel.CreateChunkBytes*2+37)
	hash := sha256.Sum256(payload)
	fence := &commonv1.ActionFence{ActionId: "media-create-original", ExecutionEpoch: 7}
	base := &mediav1.MediaPatchClaim{Intent: &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: fence.ActionId,
		Create: &mediav1.MediaCreateManifest{PayloadSha256: hash[:], PayloadBytes: uint64(len(payload))}}, Fence: fence}
	failure := errors.New("isolated media transport failure")
	for _, name := range []string{"success", "nil-claim", "nil-intent", "nil-fence", "nil-manifest", "wrong-hash", "wrong-length", "nil-context", "conflicting-auth", "open-error", "manifest-send-error", "chunk-send-error", "close-error", "nil-response", "wrong-response-manifest"} {
		t.Run("stage/"+name, func(t *testing.T) {
			claim := proto.Clone(base).(*mediav1.MediaPatchClaim)
			stream := &mediaCreateStreamFixture{failure: failure, response: &mediav1.MediaCreateStageResponse{Manifest: proto.Clone(claim.Intent.Create).(*mediav1.MediaCreateManifest)}}
			fixture := &mediaCreateClientFixture{stream: stream}
			client := &Client{tlsSecured: true, bearerAttached: true, mediaCreation: fixture}
			ctx := context.Background()
			switch name {
			case "nil-claim":
				claim = nil
			case "nil-intent":
				claim.Intent = nil
			case "nil-fence":
				claim.Fence = nil
			case "nil-manifest":
				claim.Intent.Create = nil
			case "wrong-hash":
				claim.Intent.Create.PayloadSha256[0] ^= 1
			case "wrong-length":
				claim.Intent.Create.PayloadBytes++
			case "nil-context":
				ctx = nil
			case "conflicting-auth":
				ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer conflicting"))
			case "open-error":
				fixture.openFailure = failure
			case "manifest-send-error":
				stream.failAt = 1
			case "chunk-send-error":
				stream.failAt = 2
			case "close-error":
				stream.closeFailure = failure
			case "nil-response":
				stream.response = nil
			case "wrong-response-manifest":
				stream.response.Manifest.PayloadBytes++
			}
			err := client.StageMediaCreate(ctx, claim, payload)
			if name != "success" {
				if err == nil {
					t.Fatal("invalid or failed stream accepted")
				}
				if fixture.opened > 1 {
					t.Fatal("stream was retried")
				}
				switch name {
				case "nil-claim", "nil-intent", "nil-fence", "nil-manifest", "wrong-hash", "wrong-length", "nil-context", "conflicting-auth":
					if fixture.opened != 0 {
						t.Fatal("invalid input reached transport")
					}
				}
				return
			}
			if err != nil || fixture.opened != 1 || fixture.stageOptions != 1 || len(stream.sent) != 4 {
				t.Fatal("bounded stream failed", err)
			}
			if !proto.Equal(stream.sent[0].GetManifest(), base.Intent.Create) {
				t.Fatal("manifest changed")
			}
			var received []byte
			for i, message := range stream.sent {
				if message.SchemaVersion != 1 || message.OperationId != fence.ActionId || !proto.Equal(message.Fence, fence) {
					t.Fatal("original binding changed")
				}
				if i == 0 {
					continue
				}
				chunk := message.GetChunk()
				if len(chunk) == 0 || len(chunk) > mediamodel.CreateChunkBytes {
					t.Fatal("chunk boundary changed")
				}
				received = append(received, chunk...)
			}
			if !bytes.Equal(received, payload) {
				t.Fatal("stream bytes changed")
			}
		})
	}
	value, err := mediamodel.FromJSON(map[string]any{"mediaId": "media_one", "flag": false, "zero": 0})
	if err != nil {
		t.Fatal(err)
	}
	record := value.GetObjectValue()
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	recordHash := sha256.Sum256(encoded)
	outcome := &mediav1.MediaPatchOutcome{OperationId: fence.ActionId, OriginalFence: fence,
		Created: &mediav1.MediaCreatedResult{RecordBytes: uint64(len(encoded)), RecordSha256: recordHash[:]}}
	for _, name := range []string{"success", "nil-outcome", "nil-created", "nil-fence", "nil-context", "conflicting-auth", "read-error", "nil-response", "nil-record", "invalid-encoding", "wrong-hash", "wrong-length"} {
		t.Run("read/"+name, func(t *testing.T) {
			result := proto.Clone(outcome).(*mediav1.MediaPatchOutcome)
			fixture := &mediaCreateClientFixture{response: &mediav1.ReadMediaCreateResultResponse{Media: proto.Clone(record).(*mediav1.MediaObject)}}
			client := &Client{tlsSecured: true, bearerAttached: true, mediaCreation: fixture}
			ctx := context.Background()
			switch name {
			case "nil-outcome":
				result = nil
			case "nil-created":
				result.Created = nil
			case "nil-fence":
				result.OriginalFence = nil
			case "nil-context":
				ctx = nil
			case "conflicting-auth":
				ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer conflicting"))
			case "read-error":
				fixture.readFailure = failure
			case "nil-response":
				fixture.response = nil
			case "nil-record":
				fixture.response.Media = nil
			case "invalid-encoding":
				fixture.response.Media.Members[0].Key = string([]byte{0xff})
			case "wrong-hash":
				result.Created.RecordSha256[0] ^= 1
			case "wrong-length":
				result.Created.RecordBytes++
			}
			actual, err := client.ReadMediaCreateResult(ctx, result)
			if name != "success" {
				if err == nil || actual != nil {
					t.Fatal("invalid or failed result accepted")
				}
				if fixture.read > 1 {
					t.Fatal("result read was retried")
				}
				switch name {
				case "nil-outcome", "nil-created", "nil-fence", "nil-context", "conflicting-auth":
					if fixture.read != 0 {
						t.Fatal("invalid input reached result peer")
					}
				}
				return
			}
			if err != nil || !proto.Equal(actual, record) || fixture.read != 1 || fixture.readOptions != 1 {
				t.Fatal("bound result failed", err)
			}
			if fixture.request.SchemaVersion != 1 || fixture.request.OperationId != fence.ActionId || !proto.Equal(fixture.request.OriginalFence, fence) {
				t.Fatal("result request rebound original operation")
			}
		})
	}
}
