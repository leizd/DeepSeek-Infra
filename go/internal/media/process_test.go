package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"strconv"
	"strings"
	"testing"

	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

func processIntent() *mediav1.MediaPatchIntent {
	return &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-process-" + strings.Repeat("a", 32), MediaId: "media_one",
		Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_PROCESS, Process: &mediav1.MediaProcessOptions{CorrelationId: "request.one"}}
}

func TestMediaProcessRequestBindsOptionsAndSeparatesMutationKinds(t *testing.T) {
	base := processIntent()
	digest, err := RequestDigest(base)
	if err != nil {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*mediav1.MediaPatchIntent){
		"force":           func(i *mediav1.MediaPatchIntent) { i.Process.Force = true },
		"explicit-no-ocr": func(i *mediav1.MediaPatchIntent) { i.Process.OcrEnabled = proto.Bool(false) },
		"ocr":             func(i *mediav1.MediaPatchIntent) { i.Process.OcrEnabled = proto.Bool(true) },
		"correlation":     func(i *mediav1.MediaPatchIntent) { i.Process.CorrelationId += "-next" },
		"media":           func(i *mediav1.MediaPatchIntent) { i.MediaId += "_next" },
	} {
		t.Run(name, func(t *testing.T) {
			i := proto.Clone(base).(*mediav1.MediaPatchIntent)
			change(i)
			d, e := RequestDigest(i)
			if e != nil || bytes.Equal(d, digest) {
				t.Fatal("processing options were not bound", e)
			}
		})
	}
	for name, change := range map[string]func(*mediav1.MediaPatchIntent){
		"missing-options":    func(i *mediav1.MediaPatchIntent) { i.Process = nil },
		"patch":              func(i *mediav1.MediaPatchIntent) { i.Patch = &mediav1.MediaPatch{} },
		"create":             func(i *mediav1.MediaPatchIntent) { i.Create = &mediav1.MediaCreateManifest{} },
		"old-kind":           func(i *mediav1.MediaPatchIntent) { i.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE },
		"wrong-prefix":       func(i *mediav1.MediaPatchIntent) { i.OperationId = "media-create-" + strings.Repeat("a", 32) },
		"short-nonce":        func(i *mediav1.MediaPatchIntent) { i.OperationId = "media-process-short" },
		"upper-nonce":        func(i *mediav1.MediaPatchIntent) { i.OperationId = "media-process-" + strings.Repeat("A", 32) },
		"unknown-option":     func(i *mediav1.MediaPatchIntent) { i.Process.ProtoReflect().SetUnknown([]byte{0x20, 1}) },
		"long-correlation":   func(i *mediav1.MediaPatchIntent) { i.Process.CorrelationId = strings.Repeat("a", 65) },
		"unsafe-correlation": func(i *mediav1.MediaPatchIntent) { i.Process.CorrelationId = "unsafe\ncorrelation" },
	} {
		t.Run(name, func(t *testing.T) {
			i := proto.Clone(base).(*mediav1.MediaPatchIntent)
			change(i)
			if _, e := RequestDigest(i); e == nil {
				t.Fatal("invalid process intent accepted")
			}
		})
	}
}

func TestMediaProcessSignedResultBindsSegmentsAndRejectsCrossKindResults(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	i := processIntent()
	d, err := RequestDigest(i)
	if err != nil {
		t.Fatal(err)
	}
	fence := &commonv1.ActionFence{ActionId: i.OperationId, ExecutionEpoch: 1}
	claim := &mediav1.MediaPatchClaim{Intent: i, Fence: fence, RequestSha256: d, FleetId: "fixture-fleet", Environment: "test", WriterFencingToken: 1}
	empty := sha256.Sum256(nil)
	base := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: i.OperationId, OriginalFence: fence, RequestSha256: d,
		State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{1}, 32), AfterSha256: bytes.Repeat([]byte{2}, 32), FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: 1,
		Processed: &mediav1.MediaProcessedResult{RecordSha256: bytes.Repeat([]byte{3}, 32), RecordBytes: 100, PreparationSha256: bytes.Repeat([]byte{4}, 32), OriginalFence: proto.Clone(fence).(*commonv1.ActionFence), SegmentsSha256: empty[:]}}
	sign := func(o *mediav1.MediaPatchOutcome) {
		o.Signature = nil
		b, e := Encode(o)
		if e != nil {
			t.Fatal(e)
		}
		o.Signature = ed25519.Sign(private, append(append([]byte(nil), signatureDomain...), b...))
	}
	// A present empty MediaArray has zero serialized bytes and a real SHA-256.
	sign(base)
	if err := VerifyOutcome(base, claim, public); err != nil {
		t.Fatal("valid empty segments rejected", err)
	}
	for _, status := range []uint32{200, 400, 422, 500, 599} {
		valid := proto.Clone(base).(*mediav1.MediaPatchOutcome)
		valid.HttpStatus = status
		if status != 200 {
			valid.ErrorCode = "invalid_payload"
			valid.ErrorMessage = "Original processing failure"
		}
		sign(valid)
		if err := VerifyOutcome(valid, claim, public); err != nil {
			t.Fatal(status, err)
		}
		for name, change := range map[string]func(*mediav1.MediaPatchOutcome){
			"missing-result":    func(o *mediav1.MediaPatchOutcome) { o.Processed = nil },
			"record-hash":       func(o *mediav1.MediaPatchOutcome) { o.Processed.RecordSha256 = nil },
			"segment-hash":      func(o *mediav1.MediaPatchOutcome) { o.Processed.SegmentsSha256 = nil },
			"preparation":       func(o *mediav1.MediaPatchOutcome) { o.Processed.PreparationSha256 = nil },
			"empty-record":      func(o *mediav1.MediaPatchOutcome) { o.Processed.RecordBytes = 0 },
			"oversize-segments": func(o *mediav1.MediaPatchOutcome) { o.Processed.SegmentsBytes = MaxCreatePayloadBytes + 1 },
			"epoch":             func(o *mediav1.MediaPatchOutcome) { o.Processed.OriginalFence.ExecutionEpoch++ },
			"action":            func(o *mediav1.MediaPatchOutcome) { o.Processed.OriginalFence.ActionId += "-other" },
			"created":           func(o *mediav1.MediaPatchOutcome) { o.Created = &mediav1.MediaCreatedResult{} },
			"patch":             func(o *mediav1.MediaPatchOutcome) { o.Media = &mediav1.MediaObject{} },
			"delete":            func(o *mediav1.MediaPatchOutcome) { o.DeletedCount = 1 },
			"unknown":           func(o *mediav1.MediaPatchOutcome) { o.Processed.ProtoReflect().SetUnknown([]byte{0x40, 1}) },
			"false-refusal": func(o *mediav1.MediaPatchOutcome) {
				o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
			},
			"effect-unknown": func(o *mediav1.MediaPatchOutcome) {
				o.HttpStatus = 503
				o.ErrorCode = "EFFECT_UNKNOWN"
				o.ErrorMessage = "uncertain"
			},
		} {
			t.Run(name+"-"+strconv.FormatUint(uint64(status), 10), func(t *testing.T) {
				o := proto.Clone(valid).(*mediav1.MediaPatchOutcome)
				change(o)
				sign(o)
				if VerifyOutcome(o, claim, public) == nil {
					t.Fatal("signed invalid processing result accepted")
				}
			})
		}
		valid.Processed.Indexed++
		if VerifyOutcome(valid, claim, public) == nil {
			t.Fatal("index count changed after signature")
		}
	}
}
