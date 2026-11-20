package worker

import (
	"context"
	"crypto/sha256"
	"errors"
	"io"
	"strings"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

// Keep the older forwarding fixture implementing the additive generated client.
func (f *mediaForwardingFixture) ReadPatchResult(_ context.Context, _ *mediav1.ReadMediaPatchResultRequest, _ ...grpc.CallOption) (grpc.ServerStreamingClient[mediav1.MediaPatchResultChunk], error) {
	return nil, f.failure
}

type patchResultStreamFixture struct {
	grpc.ClientStream
	chunks   []*mediav1.MediaPatchResultChunk
	terminal error
	index    int
}

func (s *patchResultStreamFixture) Recv() (*mediav1.MediaPatchResultChunk, error) {
	if s.index == len(s.chunks) {
		if s.terminal != nil {
			return nil, s.terminal
		}
		return nil, io.EOF
	}
	value := s.chunks[s.index]
	s.index++
	return value, nil
}

type patchResultClientFixture struct {
	mediav1.MediaMutationWorkerClient
	stream  *patchResultStreamFixture
	failure error
	request *mediav1.ReadMediaPatchResultRequest
	options int
}

func (f *patchResultClientFixture) ReadPatchResult(_ context.Context, r *mediav1.ReadMediaPatchResultRequest, opts ...grpc.CallOption) (grpc.ServerStreamingClient[mediav1.MediaPatchResultChunk], error) {
	f.request = proto.Clone(r).(*mediav1.ReadMediaPatchResultRequest)
	f.options = len(opts)
	return f.stream, f.failure
}

func patchResultWireFixture(t *testing.T) (*mediav1.MediaPatchedResult, []*mediav1.MediaPatchResultChunk, *mediav1.MediaObject) {
	t.Helper()
	value, err := mediamodel.FromJSON(map[string]any{"mediaId": "media_one", "metadata": map[string]any{"large": strings.Repeat("x", 5*1024*1024)}})
	if err != nil {
		t.Fatal(err)
	}
	record := value.GetObjectValue()
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(encoded)
	receipt := &mediav1.MediaPatchedResult{RecordSha256: digest[:], RecordBytes: uint64(len(encoded)), OriginalFence: &commonv1.ActionFence{ActionId: "media-patch-result", ExecutionEpoch: 1}}
	chunks := []*mediav1.MediaPatchResultChunk{{SchemaVersion: 1, Manifest: proto.Clone(receipt).(*mediav1.MediaPatchedResult)}}
	for offset := 0; offset < len(encoded); offset += mediaPatchResultChunkBytes {
		end := offset + mediaPatchResultChunkBytes
		if end > len(encoded) {
			end = len(encoded)
		}
		chunks = append(chunks, &mediav1.MediaPatchResultChunk{SchemaVersion: 1, Offset: uint64(offset), RecordChunk: append([]byte(nil), encoded[offset:end]...)})
	}
	return receipt, chunks, record
}

func TestPatchResultStreamPreservesLargeTypedRecordAndOriginalOperation(t *testing.T) {
	receipt, chunks, want := patchResultWireFixture(t)
	peer := &patchResultClientFixture{stream: &patchResultStreamFixture{chunks: chunks}}
	client := &Client{tlsSecured: true, bearerAttached: true, media: peer}
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 2, OperationId: receipt.OriginalFence.ActionId, OriginalFence: receipt.OriginalFence, Patched: receipt, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED}
	actual, err := client.ReadMediaPatchResult(context.Background(), outcome)
	if err != nil || !proto.Equal(actual, want) || peer.options != 1 || peer.request.OperationId != outcome.OperationId || !proto.Equal(peer.request.OriginalFence, outcome.OriginalFence) {
		t.Fatalf("original read binding lost: %v", err)
	}
	// Interrupted reads leave the operation unchanged and permit another READ.
	peer.stream = &patchResultStreamFixture{chunks: chunks[:2], terminal: io.ErrUnexpectedEOF}
	if _, err := client.ReadMediaPatchResult(context.Background(), outcome); !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatal("interrupted result became successful", err)
	}
	peer.stream = &patchResultStreamFixture{chunks: chunks}
	if _, err := client.ReadMediaPatchResult(context.Background(), outcome); err != nil {
		t.Fatal(err)
	}
	peer.failure = io.ErrClosedPipe
	if _, err := client.ReadMediaPatchResult(context.Background(), outcome); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatal("transport failure hidden", err)
	}
	peer.failure = nil
	badctx := metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer conflicting"))
	if _, err := client.ReadMediaPatchResult(badctx, outcome); err == nil {
		t.Fatal("credential conflict accepted")
	}
	for _, bad := range []*mediav1.MediaPatchOutcome{nil, {}, {SchemaVersion: 2, Patched: receipt}} {
		if _, err := client.ReadMediaPatchResult(context.Background(), bad); err != ErrInvalidWorkerResponse {
			t.Fatal("malformed result accepted", err)
		}
	}
	for _, bad := range []*Client{nil, New(nil), {tlsSecured: true}, {bearerAttached: true}} {
		if _, err := bad.ReadMediaPatchResult(context.Background(), outcome); err != ErrWorkerTLSRequired {
			t.Fatal("unsecured read accepted", err)
		}
	}
}

func TestPatchResultStreamRejectsTruncationExtraChunksReorderingAndTampering(t *testing.T) {
	receipt, original, _ := patchResultWireFixture(t)
	tests := map[string]func([]*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk{
		"no-manifest": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk { return c[1:] },
		"manifest-data": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[0].RecordChunk = []byte{1}
			return c
		},
		"manifest-offset": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk { c[0].Offset = 1; return c },
		"other-manifest": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[0].Manifest.RecordBytes++
			return c
		},
		"nil-first": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk { c[0] = nil; return c },
		"first-version": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[0].SchemaVersion = 2
			return c
		},
		"first-unknown": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[0].ProtoReflect().SetUnknown([]byte{0xf8, 7, 1})
			return c
		},
		"repeated-manifest": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[1].Manifest = c[0].Manifest
			return c
		},
		"offset-gap": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk { c[2].Offset++; return c },
		"duplicate": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			return append(c[:2], append([]*mediav1.MediaPatchResultChunk{c[1]}, c[2:]...)...)
		},
		"empty": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[1].RecordChunk = nil
			return c
		},
		"oversized": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[1].RecordChunk = make([]byte, mediaPatchResultChunkBytes+1)
			return c
		},
		"nil-chunk": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk { c[1] = nil; return c },
		"chunk-version": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[1].SchemaVersion = 2
			return c
		},
		"chunk-unknown": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[1].ProtoReflect().SetUnknown([]byte{0xf8, 7, 1})
			return c
		},
		"tampered": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			c[1].RecordChunk[0] ^= 1
			return c
		},
		"truncated": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk { return c[:len(c)-1] },
		"extra": func(c []*mediav1.MediaPatchResultChunk) []*mediav1.MediaPatchResultChunk {
			return append(c, &mediav1.MediaPatchResultChunk{SchemaVersion: 1, Offset: receipt.RecordBytes, RecordChunk: []byte{1}})
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			chunks := make([]*mediav1.MediaPatchResultChunk, len(original))
			for i, c := range original {
				chunks[i] = proto.Clone(c).(*mediav1.MediaPatchResultChunk)
			}
			if _, err := receiveMediaPatchResult(&patchResultStreamFixture{chunks: mutate(chunks)}, receipt); err == nil {
				t.Fatal("invalid stream accepted")
			}
		})
	}
	for _, bad := range []*mediav1.MediaPatchedResult{nil, {}, {RecordBytes: 1, RecordSha256: []byte{1}}, {RecordBytes: ^uint64(0), RecordSha256: make([]byte, 32)}} {
		if _, err := receiveMediaPatchResult(&patchResultStreamFixture{}, bad); err == nil {
			t.Fatal("malformed receipt accepted")
		}
	}
	if _, err := receiveMediaPatchResult(nil, receipt); err == nil {
		t.Fatal("nil stream accepted")
	}
	if _, err := receiveMediaPatchResult(&patchResultStreamFixture{}, receipt); err != io.EOF {
		t.Fatal("early EOF hidden", err)
	}
}

func TestPatchResultStreamValidDigestDoesNotAuthorizeMalformedTypedBytes(t *testing.T) {
	for _, payload := range [][]byte{
		{0xff},                   // A valid content digest cannot turn invalid wire bytes into a record.
		{0xf8, 0x07, 1, 0x0a, 0}, // Noncanonical ordering of unknown and known fields.
		{0x0a, 0, 0xf8, 0x07, 1}, // Canonical bytes still contain an unknown field.
		{0x0a, 0},                // A member with no typed value.
	} {
		digest := sha256.Sum256(payload)
		receipt := &mediav1.MediaPatchedResult{RecordBytes: uint64(len(payload)), RecordSha256: digest[:]}
		chunks := []*mediav1.MediaPatchResultChunk{{SchemaVersion: 1, Manifest: receipt}, {SchemaVersion: 1, RecordChunk: payload}}
		if _, err := receiveMediaPatchResult(&patchResultStreamFixture{chunks: chunks}, receipt); err == nil {
			t.Fatal("signed digest bypassed typed validation")
		}
	}
}
