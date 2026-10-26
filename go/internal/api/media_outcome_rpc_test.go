package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"testing"
)

// Signed replies below are explicit test-only producer fixtures. They qualify
// the Go control RPC boundary, not Rust custody or an actual media data effect.
func TestMediaControlRPCDispatchAndSignedSettlementRetainReplayIdentity(t *testing.T) {
	control, peer, _ := mediaWireFixture(t, "success")
	service := &mediaMutationService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}
	claim, err := control.AdmitMediaPatch(mediaRecoveryIntent("media-patch-rpc-settlement"))
	if err != nil {
		t.Fatal(err)
	}
	begin := &mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1, OperationId: claim.Intent.OperationId, Fence: claim.Fence, ClaimToken: claim.ClaimToken, WriterFencingToken: claim.WriterFencingToken, PreparationSha256: bytes.Repeat([]byte{7}, 32)}
	permit, err := service.BeginDispatch(context.Background(), begin)
	if err != nil || !permit.Fresh || permit.Claim.Intent.OperationId != claim.Intent.OperationId {
		t.Fatal("dispatch not bound", err)
	}
	replay, err := service.BeginDispatch(context.Background(), begin)
	if err != nil || replay.Fresh || !proto.Equal(replay.Claim, permit.Claim) {
		t.Fatal("fresh permit replayed", err)
	}
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: claim.Intent.OperationId, OriginalFence: claim.Fence, RequestSha256: claim.RequestSha256, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED, HttpStatus: 404, ErrorCode: "not_found", ErrorMessage: "Media absent", FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken}
	encoded, err := mediamodel.Encode(outcome)
	if err != nil {
		t.Fatal(err)
	}
	outcome.Signature = ed25519.Sign(peer.key, append([]byte("deepseek-infra:media-patch-outcome-v1\x00"), encoded...))
	request := &mediav1.RecordMediaPatchOutcomeRequest{SchemaVersion: 1, Fence: claim.Fence, Outcome: outcome}
	forged := proto.Clone(request).(*mediav1.RecordMediaPatchOutcomeRequest)
	forged.Outcome.Signature[0] ^= 1
	if _, err := service.RecordOutcome(context.Background(), forged); status.Code(err) != codes.FailedPrecondition {
		t.Fatal("untrusted proof not refused", err)
	}
	if current, err := control.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence); err != nil || current.State != "DISPATCHED" {
		t.Fatal("forged proof changed durable state", err)
	}
	wrong := proto.Clone(request).(*mediav1.RecordMediaPatchOutcomeRequest)
	wrong.Fence.ExecutionEpoch++
	if _, err := service.RecordOutcome(context.Background(), wrong); status.Code(err) != codes.InvalidArgument {
		t.Fatal("unbound outcome accepted", err)
	}
	result, err := service.RecordOutcome(context.Background(), request)
	if err != nil || result.AlreadyRecorded {
		t.Fatal("first settlement missing", err)
	}
	result, err = service.RecordOutcome(context.Background(), request)
	if err != nil || !result.AlreadyRecorded {
		t.Fatal("replayed settlement lost identity", err)
	}
	if pending, err := control.PendingMediaPatch(); err != nil || pending != nil {
		t.Fatal("signed refusal did not release admission", err)
	}
}
func TestMediaControlRPCRefusesBothMutationMethodsWithoutAuthority(t *testing.T) {
	control, _, _ := mediaWireFixture(t, "success")
	service := &mediaMutationService{control: control, health: ShadowHealth()}
	if _, err := service.BeginDispatch(context.Background(), nil); status.Code(err) != codes.PermissionDenied {
		t.Fatal("shadow dispatch accepted", err)
	}
	if _, err := service.RecordOutcome(context.Background(), nil); status.Code(err) != codes.PermissionDenied {
		t.Fatal("shadow settlement accepted", err)
	}
	service.health = HealthFor(config.Config{Mode: config.ModeAuthoritative})
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python")
	if _, err := service.BeginDispatch(context.Background(), nil); status.Code(err) != codes.PermissionDenied {
		t.Fatal("legacy-mode dispatch accepted", err)
	}
	if _, err := service.RecordOutcome(context.Background(), nil); status.Code(err) != codes.PermissionDenied {
		t.Fatal("legacy-mode settlement accepted", err)
	}
}

func TestMediaLegacyReadRPCStillDefaultsToShadowAndRejectsMalformedSnapshots(t *testing.T) {
	service := &controlReadService{}
	health, err := service.Health(context.Background(), &controlv1.HealthRequest{})
	if err != nil || health.Mode != controlv1.RuntimeMode_RUNTIME_MODE_SHADOW_READONLY || !health.Ok {
		t.Fatal("unconfigured control gained authority", err)
	}
	if _, err := service.ShadowEvaluate(context.Background(), &controlv1.ShadowEvaluateRequest{CanonicalInput: []byte("{")}); status.Code(err) != codes.InvalidArgument {
		t.Fatal("malformed shadow snapshot accepted", err)
	}
}
