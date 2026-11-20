package worker

import (
	"context"
	"crypto/sha256"
	"errors"
	"io"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

// ReadFlatMediaPatchResult is the flat-record sibling of ReadMediaPatchResult: it
// streams the same chunked record but answers with the original flat JSON instead
// of the typed object. Its guards are what stop a caller from reading a result
// under the wrong schema version, from a non-applied outcome, or through an
// unsecured client, so each one is exercised here rather than left to the typed
// path's coverage.

func flatPatchResultFixture(t *testing.T) (*mediav1.MediaPatchedResult, []*mediav1.MediaPatchResultChunk, []byte) {
	t.Helper()
	record := &mediav1.MediaFlatPatchRecord{Nodes: []*mediav1.MediaFlatPatchNode{
		{Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: "retained"}},
		{Kind: &mediav1.MediaFlatPatchNode_ObjectValue{ObjectValue: &mediav1.MediaFlatPatchObject{
			Members: []*mediav1.MediaFlatPatchMember{{Key: "title", Child: 1}},
		}}},
	}}
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	want, err := mediamodel.FlatPatchJSON(encoded)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(encoded)
	receipt := &mediav1.MediaPatchedResult{
		RecordSha256:  digest[:],
		RecordBytes:   uint64(len(encoded)),
		OriginalFence: &commonv1.ActionFence{ActionId: "media-patch-flat", ExecutionEpoch: 1},
	}
	chunks := []*mediav1.MediaPatchResultChunk{{SchemaVersion: 1, Manifest: proto.Clone(receipt).(*mediav1.MediaPatchedResult)}}
	chunks = append(chunks, &mediav1.MediaPatchResultChunk{SchemaVersion: 1, Offset: 0, RecordChunk: append([]byte(nil), encoded...)})
	return receipt, chunks, want
}

func TestReadFlatMediaPatchResultReturnsOriginalFlatJSON(t *testing.T) {
	receipt, chunks, want := flatPatchResultFixture(t)
	peer := &patchResultClientFixture{stream: &patchResultStreamFixture{chunks: chunks}}
	client := &Client{tlsSecured: true, bearerAttached: true, media: peer}
	outcome := &mediav1.MediaPatchOutcome{
		SchemaVersion: mediamodel.FlatPatchOutcomeVersion,
		OperationId:   receipt.OriginalFence.ActionId,
		OriginalFence: receipt.OriginalFence,
		Patched:       receipt,
		State:         mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
	}
	actual, err := client.ReadFlatMediaPatchResult(context.Background(), outcome)
	if err != nil {
		t.Fatal(err)
	}
	if string(actual) != string(want) {
		t.Fatalf("flat result changed the original record: %s", actual)
	}
	if peer.request.OperationId != outcome.OperationId || !proto.Equal(peer.request.OriginalFence, outcome.OriginalFence) {
		t.Fatal("flat read lost the original operation binding")
	}
}

func TestReadFlatMediaPatchResultRefusesMalformedOutcomes(t *testing.T) {
	receipt, chunks, _ := flatPatchResultFixture(t)
	peer := &patchResultClientFixture{stream: &patchResultStreamFixture{chunks: chunks}}
	client := &Client{tlsSecured: true, bearerAttached: true, media: peer}

	// A compact-schema outcome must not be read as flat, and vice versa: the two
	// decoders answer with different shapes from the same wire bytes.
	compact := &mediav1.MediaPatchOutcome{
		SchemaVersion: mediamodel.CompactPatchOutcomeVersion,
		OperationId:   receipt.OriginalFence.ActionId,
		OriginalFence: receipt.OriginalFence,
		Patched:       receipt,
		State:         mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
	}
	if _, err := client.ReadFlatMediaPatchResult(context.Background(), compact); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatalf("compact outcome was read as flat: %v", err)
	}
	for name, outcome := range map[string]*mediav1.MediaPatchOutcome{
		"nil":         nil,
		"no-patched":  {SchemaVersion: mediamodel.FlatPatchOutcomeVersion, OperationId: "x", OriginalFence: receipt.OriginalFence, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED},
		"no-fence":    {SchemaVersion: mediamodel.FlatPatchOutcomeVersion, OperationId: "x", Patched: receipt, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED},
		"not-applied": {SchemaVersion: mediamodel.FlatPatchOutcomeVersion, OperationId: "x", OriginalFence: receipt.OriginalFence, Patched: receipt, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED},
	} {
		if _, err := client.ReadFlatMediaPatchResult(context.Background(), outcome); !errors.Is(err, ErrInvalidWorkerResponse) {
			t.Fatalf("%s flat outcome was accepted: %v", name, err)
		}
	}
}

func TestReadFlatMediaPatchResultRefusesAnUnsecuredClient(t *testing.T) {
	receipt, chunks, _ := flatPatchResultFixture(t)
	outcome := &mediav1.MediaPatchOutcome{
		SchemaVersion: mediamodel.FlatPatchOutcomeVersion,
		OperationId:   receipt.OriginalFence.ActionId,
		OriginalFence: receipt.OriginalFence,
		Patched:       receipt,
		State:         mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
	}
	peer := &patchResultClientFixture{stream: &patchResultStreamFixture{chunks: chunks}}
	for name, client := range map[string]*Client{
		"nil":             nil,
		"no-tls":          {bearerAttached: true, media: peer},
		"no-bearer":       {tlsSecured: true, media: peer},
		"no-media-client": {tlsSecured: true, bearerAttached: true},
	} {
		if _, err := client.ReadFlatMediaPatchResult(context.Background(), outcome); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatalf("%s client read a flat result: %v", name, err)
		}
	}
}

func TestReadFlatMediaPatchResultPropagatesStreamFailures(t *testing.T) {
	receipt, chunks, _ := flatPatchResultFixture(t)
	outcome := &mediav1.MediaPatchOutcome{
		SchemaVersion: mediamodel.FlatPatchOutcomeVersion,
		OperationId:   receipt.OriginalFence.ActionId,
		OriginalFence: receipt.OriginalFence,
		Patched:       receipt,
		State:         mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
	}
	// A stream that dies mid-record must surface the transport error instead of
	// decoding whatever arrived.
	peer := &patchResultClientFixture{stream: &patchResultStreamFixture{chunks: chunks[:1], terminal: io.ErrUnexpectedEOF}}
	client := &Client{tlsSecured: true, bearerAttached: true, media: peer}
	if _, err := client.ReadFlatMediaPatchResult(context.Background(), outcome); !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("interrupted flat read became successful: %v", err)
	}
	// A dial failure must surface too.
	peer.stream = &patchResultStreamFixture{chunks: chunks}
	peer.failure = io.ErrClosedPipe
	if _, err := client.ReadFlatMediaPatchResult(context.Background(), outcome); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("flat dial failure hidden: %v", err)
	}
}
