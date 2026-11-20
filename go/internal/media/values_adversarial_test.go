package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
	"strings"
	"testing"
)

func TestMediaNestedMalformedValuesCannotBypassTheTypedBoundary(t *testing.T) {
	for _, value := range []any{json.Number("NaN"), json.Number("[]"), map[string]any{"nested": make(chan bool)}} {
		if _, err := FromJSON(value); err == nil {
			t.Fatalf("accepted malformed nested value %T", value)
		}
	}
	for name, value := range map[string]*mediav1.MediaValue{
		"object-child": {Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: &mediav1.MediaObject{Members: []*mediav1.MediaMember{{Key: "bad", Value: nil}}}}},
		"array-child":  {Kind: &mediav1.MediaValue_ArrayValue{ArrayValue: &mediav1.MediaArray{Values: []*mediav1.MediaValue{nil}}}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := ToJSON(value); err == nil {
				t.Fatal("accepted malformed child")
			}
		})
	}
	nested := &mediav1.MediaValue{Kind: &mediav1.MediaValue_NullValue{NullValue: true}}
	for i := 0; i < 130; i++ {
		nested = &mediav1.MediaValue{Kind: &mediav1.MediaValue_ArrayValue{ArrayValue: &mediav1.MediaArray{Values: []*mediav1.MediaValue{nested}}}}
	}
	if _, err := ToJSON(nested); err == nil {
		t.Fatal("accepted excessive wire depth")
	}
}

func TestMediaDigestRejectsUnknownFieldsInvalidUTF8AndOversizedMessages(t *testing.T) {
	value, _ := FromJSON("title")
	base := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-patch-adversarial", MediaId: "media_one", Patch: &mediav1.MediaPatch{Title: value}}
	for name, mutate := range map[string]func(*mediav1.MediaPatchIntent){
		"unknown-intent": func(i *mediav1.MediaPatchIntent) { i.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"unknown-patch":  func(i *mediav1.MediaPatchIntent) { i.Patch.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"invalid-value":  func(i *mediav1.MediaPatchIntent) { i.Patch.Title = &mediav1.MediaValue{} },
		"invalid-utf8":   func(i *mediav1.MediaPatchIntent) { i.OperationId = string([]byte{0xff}) },
		"oversized": func(i *mediav1.MediaPatchIntent) {
			i.Patch.Title = &mediav1.MediaValue{Kind: &mediav1.MediaValue_TextValue{TextValue: strings.Repeat("x", MaxMessageBytes+1)}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			i := proto.Clone(base).(*mediav1.MediaPatchIntent)
			mutate(i)
			if _, err := RequestDigest(i); err == nil {
				t.Fatal("unencodable or ambiguous intent received a digest")
			}
		})
	}
	var absent *mediav1.MediaPatchIntent
	if _, err := Encode(absent); err == nil {
		t.Fatal("accepted typed nil protobuf")
	}
	if _, err := Encode(&mediav1.MediaValue{Kind: &mediav1.MediaValue_TextValue{TextValue: string([]byte{0xff})}}); err == nil {
		t.Fatal("invalid UTF-8 encoded")
	}
}

func TestMediaOutcomeRejectsMalformedAppliedObjectsAndRefusalEncoding(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	claim := &mediav1.MediaPatchClaim{Intent: &mediav1.MediaPatchIntent{OperationId: "media-patch-proof"}, Fence: &commonv1.ActionFence{ActionId: "media-patch-proof", ExecutionEpoch: 1}, RequestSha256: bytes.Repeat([]byte{1}, 32), FleetId: "fixture", Environment: "isolated", WriterFencingToken: 1}
	base := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: claim.Intent.OperationId, OriginalFence: claim.Fence, RequestSha256: claim.RequestSha256, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED, HttpStatus: 404, ErrorCode: "not_found", ErrorMessage: "absent", FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: 1}
	sign := func(o *mediav1.MediaPatchOutcome) {
		t.Helper()
		o.Signature = nil
		b, e := Encode(o)
		if e != nil {
			t.Fatal(e)
		}
		o.Signature = ed25519.Sign(private, append(append([]byte(nil), signatureDomain...), b...))
	}
	sign(base)
	if err := VerifyOutcome(base, claim, public); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*mediav1.MediaPatchOutcome){
		"invalid-refusal-status": func(o *mediav1.MediaPatchOutcome) { o.HttpStatus = 503 },
		"refusal-with-effect":    func(o *mediav1.MediaPatchOutcome) { o.AfterSha256 = bytes.Repeat([]byte{3}, 32) },
		"invalid-refusal-utf8":   func(o *mediav1.MediaPatchOutcome) { o.ErrorMessage = string([]byte{0xff}) },
		"malformed-applied-object": func(o *mediav1.MediaPatchOutcome) {
			o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED
			o.HttpStatus = 200
			o.ErrorCode = ""
			o.ErrorMessage = ""
			o.BeforeSha256 = bytes.Repeat([]byte{2}, 32)
			o.AfterSha256 = bytes.Repeat([]byte{3}, 32)
			o.Media = &mediav1.MediaObject{Members: []*mediav1.MediaMember{{Key: "invalid", Value: nil}}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			o := proto.Clone(base).(*mediav1.MediaPatchOutcome)
			mutate(o)
			if VerifyOutcome(o, claim, public) == nil {
				t.Fatal("malformed outcome accepted")
			}
		})
	}
}
