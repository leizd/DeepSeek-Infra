package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
	"net/http"
	"testing"
)

// A simulated peer tests Go refusal boundaries. Production key custody is
// exercised independently by the real native-launcher workload.
func TestMediaCreateSignedResultRequiresOriginalDigestFenceAndShape(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	intent := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-create-fixture", MediaId: "media_one", Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_CREATE,
		Create: &mediav1.MediaCreateManifest{PayloadSha256: bytes.Repeat([]byte{1}, 32), PayloadBytes: 100}}
	digest, err := RequestDigest(intent)
	if err != nil {
		t.Fatal(err)
	}
	fence := &commonv1.ActionFence{ActionId: intent.OperationId, ExecutionEpoch: 1}
	claim := &mediav1.MediaPatchClaim{Intent: intent, Fence: fence, RequestSha256: digest, FleetId: "fixture-fleet", Environment: "test", WriterFencingToken: 1}
	base := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: intent.OperationId, OriginalFence: fence, RequestSha256: digest,
		State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{2}, 32), AfterSha256: bytes.Repeat([]byte{3}, 32),
		FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken,
		Created: &mediav1.MediaCreatedResult{RecordSha256: bytes.Repeat([]byte{4}, 32), RecordBytes: 100, PreparationSha256: bytes.Repeat([]byte{5}, 32), OriginalFence: proto.Clone(fence).(*commonv1.ActionFence)}}
	sign := func(outcome *mediav1.MediaPatchOutcome) {
		outcome.Signature = nil
		encoded, e := Encode(outcome)
		if e != nil {
			t.Fatal(e)
		}
		outcome.Signature = ed25519.Sign(private, append(append([]byte(nil), signatureDomain...), encoded...))
	}
	sign(base)
	if err := VerifyOutcome(base, claim, public); err != nil {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*mediav1.MediaPatchOutcome){
		"missing-result":       func(o *mediav1.MediaPatchOutcome) { o.Created = nil },
		"missing-record-hash":  func(o *mediav1.MediaPatchOutcome) { o.Created.RecordSha256 = nil },
		"missing-preparation":  func(o *mediav1.MediaPatchOutcome) { o.Created.PreparationSha256 = nil },
		"empty-record":         func(o *mediav1.MediaPatchOutcome) { o.Created.RecordBytes = 0 },
		"future-result-fence":  func(o *mediav1.MediaPatchOutcome) { o.Created.OriginalFence.ExecutionEpoch = 2 },
		"foreign-result-fence": func(o *mediav1.MediaPatchOutcome) { o.Created.OriginalFence.ActionId = "other" },
		"mixed-patch":          func(o *mediav1.MediaPatchOutcome) { o.Media = &mediav1.MediaObject{} },
		"mixed-delete":         func(o *mediav1.MediaPatchOutcome) { o.DeletedCount = 1 },
		"mixed-process":        func(o *mediav1.MediaPatchOutcome) { o.Processed = &mediav1.MediaProcessedResult{} },
		"wrong-intent":         func(o *mediav1.MediaPatchOutcome) { o.RequestSha256[0] ^= 1 },
	} {
		t.Run(name, func(t *testing.T) {
			outcome := proto.Clone(base).(*mediav1.MediaPatchOutcome)
			change(outcome)
			sign(outcome)
			if VerifyOutcome(outcome, claim, public) == nil {
				t.Fatal("signed invalid creation accepted")
			}
		})
	}
	for _, status := range []uint32{400, 422, 500, 599} {
		t.Run("applied-processing-failure-"+http.StatusText(int(status)), func(t *testing.T) {
			failure := proto.Clone(base).(*mediav1.MediaPatchOutcome)
			failure.HttpStatus, failure.ErrorCode, failure.ErrorMessage = status, "invalid_payload", "Original processing failure"
			sign(failure)
			if err := VerifyOutcome(failure, claim, public); err != nil {
				t.Fatal("applied failed record/source was rejected as if it had no effect", err)
			}
			for name, change := range map[string]func(*mediav1.MediaPatchOutcome){
				"no-record":      func(o *mediav1.MediaPatchOutcome) { o.Created = nil },
				"no-code":        func(o *mediav1.MediaPatchOutcome) { o.ErrorCode = "" },
				"unknown-effect": func(o *mediav1.MediaPatchOutcome) { o.ErrorCode = "EFFECT_UNKNOWN" },
				"no-message":     func(o *mediav1.MediaPatchOutcome) { o.ErrorMessage = "" },
				"false-success":  func(o *mediav1.MediaPatchOutcome) { o.HttpStatus = 200 },
				"non-error":      func(o *mediav1.MediaPatchOutcome) { o.HttpStatus = 399 },
				"invalid-status": func(o *mediav1.MediaPatchOutcome) { o.HttpStatus = 600 },
				"false-absence": func(o *mediav1.MediaPatchOutcome) {
					o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
				},
				"foreign-epoch": func(o *mediav1.MediaPatchOutcome) { o.Created.OriginalFence.ExecutionEpoch++ },
			} {
				t.Run(name, func(t *testing.T) {
					invalid := proto.Clone(failure).(*mediav1.MediaPatchOutcome)
					change(invalid)
					sign(invalid)
					if VerifyOutcome(invalid, claim, public) == nil {
						t.Fatal("invalid processing failure accepted")
					}
				})
			}
			failure.ErrorMessage += " tampered"
			if VerifyOutcome(failure, claim, public) == nil {
				t.Fatal("processing error mutated after signing")
			}
		})
	}
	base.Created.RecordSha256[0] ^= 1
	if VerifyOutcome(base, claim, public) == nil {
		t.Fatal("record digest was mutable after signing")
	}
}
