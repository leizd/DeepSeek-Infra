package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"testing"

	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

// These ephemeral keys simulate signed Rust evidence only. Production private
// keys remain in Rust custody; this fixture cannot grant an admission or effect.
func TestMediaDeleteSignedCountsCannotRebindPatchOrRefusal(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	intent := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-delete-signed", MediaId: "media_one", Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE}
	digest, err := RequestDigest(intent)
	if err != nil {
		t.Fatal(err)
	}
	claim := &mediav1.MediaPatchClaim{Intent: intent, Fence: &commonv1.ActionFence{ActionId: intent.OperationId, ExecutionEpoch: 1}, RequestSha256: digest, FleetId: "fixture-fleet", Environment: "test", WriterFencingToken: 7}
	base := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: intent.OperationId, OriginalFence: claim.Fence, RequestSha256: digest, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{1}, 32), AfterSha256: bytes.Repeat([]byte{2}, 32), FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken, DeletedCount: 1}
	sign := func(outcome *mediav1.MediaPatchOutcome) {
		outcome.Signature = nil
		data, encodeErr := Encode(outcome)
		if encodeErr != nil {
			t.Fatal(encodeErr)
		}
		outcome.Signature = ed25519.Sign(private, append(append([]byte(nil), signatureDomain...), data...))
	}
	for _, count := range []uint32{0, 1} {
		outcome := proto.Clone(base).(*mediav1.MediaPatchOutcome)
		outcome.DeletedCount = count
		sign(outcome)
		if err := VerifyOutcome(outcome, claim, public); err != nil {
			t.Fatalf("valid original count %d refused: %v", count, err)
		}
		outcome.DeletedCount ^= 1
		if VerifyOutcome(outcome, claim, public) == nil {
			t.Fatal("signed count was mutable")
		}
	}
	for name, change := range map[string]func(*mediav1.MediaPatchOutcome){
		"too-many":         func(o *mediav1.MediaPatchOutcome) { o.DeletedCount = 2 },
		"unexpected-media": func(o *mediav1.MediaPatchOutcome) { o.Media = &mediav1.MediaObject{} },
		"refusal-count": func(o *mediav1.MediaPatchOutcome) {
			o.State, o.HttpStatus, o.ErrorCode, o.AfterSha256 = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED, 409, "conflict", nil
		},
	} {
		t.Run(name, func(t *testing.T) {
			outcome := proto.Clone(base).(*mediav1.MediaPatchOutcome)
			change(outcome)
			sign(outcome)
			if VerifyOutcome(outcome, claim, public) == nil {
				t.Fatal("invalid signed deletion shape accepted")
			}
		})
	}
	refusal := proto.Clone(base).(*mediav1.MediaPatchOutcome)
	refusal.State, refusal.HttpStatus, refusal.ErrorCode, refusal.AfterSha256, refusal.DeletedCount = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED, 409, "conflict", nil, 0
	sign(refusal)
	if err := VerifyOutcome(refusal, claim, public); err != nil {
		t.Fatal("well-formed deletion refusal rejected", err)
	}
	patchClaim := proto.Clone(claim).(*mediav1.MediaPatchClaim)
	patchClaim.Intent.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED
	sign(base)
	if VerifyOutcome(base, patchClaim, public) == nil {
		t.Fatal("deletion outcome rebound as PATCH")
	}
	unknown := proto.Clone(claim).(*mediav1.MediaPatchClaim)
	unknown.Intent.Kind = mediav1.MediaMutationKind(2)
	if VerifyOutcome(base, unknown, public) == nil {
		t.Fatal("unknown operation kind accepted")
	}
}
