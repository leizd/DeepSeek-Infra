package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

func TestCompactPatchSettlementRequiresTheExactAdmittedDispatchPlan(t *testing.T) {
	for _, correct := range []bool{false, true} {
		t.Run(map[bool]string{false: "different-signed-plan", true: "matching-plan"}[correct], func(t *testing.T) {
			control, now, key := mediaControl(t)
			intent := mediaIntent("media-patch-compact-store")
			intent.MaxOutcomeSchemaVersion = 2
			claim, err := control.AdmitMediaPatch(intent)
			if err != nil {
				t.Fatal(err)
			}
			record := sha256.Sum256([]byte("original typed result"))
			preparation, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&mediav1.MediaPatchPreparation{SchemaVersion: 1, Claim: claim, RecordSha256: record[:], RecordBytes: 21})
			if err != nil {
				t.Fatal(err)
			}
			recordPreparation := sha256.Sum256(preparation)
			outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 2, OperationId: intent.OperationId, OriginalFence: claim.Fence, RequestSha256: claim.RequestSha256, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{1}, 32), AfterSha256: bytes.Repeat([]byte{2}, 32), FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken, Patched: &mediav1.MediaPatchedResult{RecordSha256: record[:], RecordBytes: 21, PreparationSha256: recordPreparation[:], OriginalFence: claim.Fence}}
			unsigned, err := mediamodel.Encode(outcome)
			if err != nil {
				t.Fatal(err)
			}
			dispatch := sha256.Sum256(unsigned)
			request := mediaBegin(claim)
			request.PreparationSha256 = dispatch[:]
			if !correct {
				request.PreparationSha256 = bytes.Repeat([]byte{8}, 32)
			}
			if _, err := control.BeginMediaPatchDispatch(request); err != nil {
				t.Fatal(err)
			}
			outcome.Signature = ed25519.Sign(key, append([]byte("deepseek-infra:media-patch-outcome-v2\x00"), unsigned...))
			*now = claim.LeaseUntilUnix + 1 // Settlement of the original signed result survives expiry.
			if _, err := control.RecordMediaPatchOutcome(outcome); correct && err != nil || !correct && err == nil {
				t.Fatalf("dispatch identity check: correct=%t err=%v", correct, err)
			}
			retained, err := control.GetMediaPatchClaim(intent.OperationId, claim.Fence)
			if err != nil {
				t.Fatal(err)
			}
			want := "DISPATCHED"
			if correct {
				want = "APPLIED"
			}
			if retained.State != want {
				t.Fatalf("terminal state=%s want=%s", retained.State, want)
			}
			if correct {
				if already, err := control.RecordMediaPatchOutcome(outcome); err != nil || !already {
					t.Fatal("original receipt replay changed", err)
				}
			}
		})
	}
}
