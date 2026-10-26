package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
	"reflect"
	"testing"
)

func TestMediaValuesRetainNullNumbersNestedArraysAndUnicode(t *testing.T) {
	d := json.NewDecoder(bytes.NewBufferString(`{"":"empty key","null":null,"false":false,"number":-1.25e+7,"tree":[{"中文":"📁"},[],"literal"]}`))
	d.UseNumber()
	var input any
	if err := d.Decode(&input); err != nil {
		t.Fatal(err)
	}
	typed, err := FromJSON(input)
	if err != nil {
		t.Fatal(err)
	}
	actual, err := ToJSON(typed)
	if err != nil || !reflect.DeepEqual(actual, input) {
		t.Fatalf("roundtrip = %#v, %v", actual, err)
	}
	if typed.GetObjectValue().Members[0].Key != "" {
		t.Fatal("members must retain sorted UTF-8 keys")
	}
	for _, n := range []string{"0", "-0", "1.0", "1e99", "-1.2E-5"} {
		value, err := ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_NumberValue{NumberValue: n}})
		if err != nil || value != json.Number(n) {
			t.Fatalf("number %s: %#v %v", n, value, err)
		}
	}
}
func TestMediaValuesRejectAmbiguousOrMalformedWireValues(t *testing.T) {
	values := []*mediav1.MediaValue{nil, {}, {Kind: &mediav1.MediaValue_NullValue{NullValue: false}}, {Kind: &mediav1.MediaValue_ObjectValue{}}, {Kind: &mediav1.MediaValue_ArrayValue{}},
		{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: &mediav1.MediaObject{Members: []*mediav1.MediaMember{nil}}}},
		{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: &mediav1.MediaObject{Members: []*mediav1.MediaMember{{Key: "same", Value: &mediav1.MediaValue{Kind: &mediav1.MediaValue_BoolValue{BoolValue: true}}}, {Key: "same", Value: &mediav1.MediaValue{Kind: &mediav1.MediaValue_BoolValue{BoolValue: false}}}}}}},
	}
	for _, n := range []string{"", "NaN", "Infinity", "01", "true", "null", "[]", "{}", `"1"`, "1 2"} {
		values = append(values, &mediav1.MediaValue{Kind: &mediav1.MediaValue_NumberValue{NumberValue: n}})
	}
	for i, v := range values {
		if _, err := ToJSON(v); err == nil {
			t.Fatalf("accepted malformed value %d", i)
		}
	}
	if _, err := FromJSON(make(chan bool)); err == nil {
		t.Fatal("accepted foreign type")
	}
	var nested any = "leaf"
	for i := 0; i < 130; i++ {
		nested = []any{nested}
	}
	if _, err := FromJSON(nested); err == nil {
		t.Fatal("accepted excessive nesting")
	}
	v := &mediav1.MediaValue{Kind: &mediav1.MediaValue_BoolValue{BoolValue: true}}
	v.ProtoReflect().SetUnknown([]byte{0x78, 0x01})
	if _, err := ToJSON(v); err == nil {
		t.Fatal("accepted unknown wire field")
	}
}
func TestMediaDigestBindsPresenceAndEveryIntentField(t *testing.T) {
	text, _ := FromJSON("hello")
	intent := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-patch-fixture", MediaId: "media_one", Patch: &mediav1.MediaPatch{Title: text}}
	digest, err := RequestDigest(intent)
	if err != nil {
		t.Fatal(err)
	}
	for _, mutate := range []func(*mediav1.MediaPatchIntent){func(i *mediav1.MediaPatchIntent) { i.OperationId += "-other" }, func(i *mediav1.MediaPatchIntent) { i.MediaId += "-other" }, func(i *mediav1.MediaPatchIntent) {
		i.Patch.Metadata = &mediav1.MediaValue{Kind: &mediav1.MediaValue_NullValue{NullValue: true}}
	}} {
		other := proto.Clone(intent).(*mediav1.MediaPatchIntent)
		mutate(other)
		actual, err := RequestDigest(other)
		if err != nil || bytes.Equal(actual, digest) {
			t.Fatal("intent identity failed to bind change", err)
		}
	}
	for _, bad := range []*mediav1.MediaPatchIntent{nil, {}, {SchemaVersion: 1, OperationId: "op", MediaId: "id", Patch: &mediav1.MediaPatch{}}} {
		if _, err := RequestDigest(bad); err == nil {
			t.Fatal("accepted incomplete intent")
		}
	}
	if _, err := Encode(nil); err == nil {
		t.Fatal("accepted nil message")
	}
}
func TestMediaSignedOutcomeCannotRebindAnOperationOrRealm(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	value, _ := FromJSON(map[string]any{"mediaId": "media_one", "title": "hello"})
	claim := &mediav1.MediaPatchClaim{Intent: &mediav1.MediaPatchIntent{OperationId: "media-patch-fixture"}, Fence: &commonv1.ActionFence{ActionId: "media-patch-fixture", ExecutionEpoch: 1}, RequestSha256: bytes.Repeat([]byte{1}, 32), FleetId: "fixture-fleet", Environment: "isolated", WriterFencingToken: 7}
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: claim.Intent.OperationId, OriginalFence: claim.Fence, RequestSha256: claim.RequestSha256, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, Media: value.GetObjectValue(), HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{2}, 32), AfterSha256: bytes.Repeat([]byte{3}, 32), FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: 7}
	sign := func(o *mediav1.MediaPatchOutcome) {
		o.Signature = nil
		b, err := Encode(o)
		if err != nil {
			t.Fatal(err)
		}
		o.Signature = ed25519.Sign(private, append(append([]byte(nil), signatureDomain...), b...))
	}
	sign(outcome)
	if err := VerifyOutcome(outcome, claim, public); err != nil {
		t.Fatal(err)
	}
	for _, mutate := range []func(*mediav1.MediaPatchOutcome){func(o *mediav1.MediaPatchOutcome) { o.OperationId = "another" }, func(o *mediav1.MediaPatchOutcome) { o.OriginalFence.ExecutionEpoch++ }, func(o *mediav1.MediaPatchOutcome) { o.FleetId = "foreign" }, func(o *mediav1.MediaPatchOutcome) { o.Environment = "foreign" }, func(o *mediav1.MediaPatchOutcome) { o.WriterFencingToken++ }, func(o *mediav1.MediaPatchOutcome) { o.RequestSha256[0]++ }, func(o *mediav1.MediaPatchOutcome) { o.HttpStatus = 201 }, func(o *mediav1.MediaPatchOutcome) { o.Media = nil }, func(o *mediav1.MediaPatchOutcome) {
		o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_UNKNOWN
	}, func(o *mediav1.MediaPatchOutcome) { o.Signature[0] ^= 1 }} {
		other := proto.Clone(outcome).(*mediav1.MediaPatchOutcome)
		mutate(other)
		if VerifyOutcome(other, claim, public) == nil {
			t.Fatal("accepted altered outcome")
		}
	}
	refused := proto.Clone(outcome).(*mediav1.MediaPatchOutcome)
	refused.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
	refused.Media = nil
	refused.HttpStatus = 404
	refused.ErrorCode = "not_found"
	refused.ErrorMessage = "Media not found"
	refused.AfterSha256 = nil
	sign(refused)
	if err := VerifyOutcome(refused, claim, public); err != nil {
		t.Fatal(err)
	}
}
