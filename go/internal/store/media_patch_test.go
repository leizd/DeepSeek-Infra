package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
	"path/filepath"
	"testing"
)

func mediaControl(t *testing.T) (*Control, *int64, ed25519.PrivateKey) {
	t.Helper()
	public, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := int64(1000)
	c, err := OpenControl(OpenOptions{Path: filepath.Join(t.TempDir(), "control"), Owner: "media-test", LeaseSeconds: 3600, Now: func() int64 { return now }, MediaOutcomeSignerPublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "media-fleet", Environment: "isolated"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return c, &now, key
}
func mediaIntent(operation string) *mediav1.MediaPatchIntent {
	v, _ := mediamodel.FromJSON("changed")
	return &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: operation, MediaId: "media_one", Patch: &mediav1.MediaPatch{Title: v}}
}
func mediaBegin(c *mediav1.MediaPatchClaim) *mediav1.BeginMediaPatchDispatchRequest {
	return &mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1, OperationId: c.Intent.OperationId, Fence: c.Fence, ClaimToken: c.ClaimToken, WriterFencingToken: c.WriterFencingToken, PreparationSha256: bytes.Repeat([]byte{7}, 32)}
}
func mediaProof(t *testing.T, c *mediav1.MediaPatchClaim, key ed25519.PrivateKey) *mediav1.MediaPatchOutcome {
	t.Helper()
	v, _ := mediamodel.FromJSON(map[string]any{"mediaId": c.Intent.MediaId, "title": "changed"})
	o := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: c.Intent.OperationId, OriginalFence: proto.Clone(c.Fence).(*commonv1.ActionFence), RequestSha256: c.RequestSha256, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, Media: v.GetObjectValue(), HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{1}, 32), AfterSha256: bytes.Repeat([]byte{2}, 32), FleetId: c.FleetId, Environment: c.Environment, WriterFencingToken: c.WriterFencingToken}
	signMediaProof(t, o, key)
	return o
}
func signMediaProof(t *testing.T, o *mediav1.MediaPatchOutcome, key ed25519.PrivateKey) {
	t.Helper()
	o.Signature = nil
	b, err := mediamodel.Encode(o)
	if err != nil {
		t.Fatal(err)
	}
	o.Signature = ed25519.Sign(key, append([]byte("deepseek-infra:media-patch-outcome-v1\x00"), b...))
}
func TestMediaAdmissionSerializesTheLibraryAndRetainsOriginalIdentity(t *testing.T) {
	c, _, _ := mediaControl(t)
	intent := mediaIntent("media-patch-one")
	claim, err := c.AdmitMediaPatch(intent)
	if err != nil {
		t.Fatal(err)
	}
	same, err := c.AdmitMediaPatch(intent)
	if err != nil || !proto.Equal(same, claim) {
		t.Fatal("replay changed the claim", err)
	}
	changed := proto.Clone(intent).(*mediav1.MediaPatchIntent)
	changed.MediaId = "other"
	if _, err := c.AdmitMediaPatch(changed); !errors.Is(err, ErrRevisionConflict) {
		t.Fatal(err)
	}
	if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-two")); !errors.Is(err, ErrMediaEffectUnknown) {
		t.Fatal("concurrent publication admitted", err)
	}
	actual, err := c.GetMediaPatchClaim(intent.OperationId, claim.Fence)
	if err != nil || !proto.Equal(actual, claim) {
		t.Fatal(err)
	}
	wrong := proto.Clone(claim.Fence).(*commonv1.ActionFence)
	wrong.ExecutionEpoch++
	if _, err := c.GetMediaPatchClaim(intent.OperationId, wrong); !errors.Is(err, ErrMediaClaimDenied) {
		t.Fatal("future epoch admitted", err)
	}
	if err := c.CancelUndispatchedMediaPatch(intent.OperationId); err != nil {
		t.Fatal(err)
	}
	if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); err == nil {
		t.Fatal("cancelled claim dispatched")
	}
	if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-two")); err != nil {
		t.Fatal(err)
	}
}
func TestMediaDispatchRejectsStaleClaimsAndNeverReissuesAFreshPermit(t *testing.T) {
	c, now, _ := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-one"))
	if err != nil {
		t.Fatal(err)
	}
	for _, mutate := range []func(*mediav1.BeginMediaPatchDispatchRequest){func(r *mediav1.BeginMediaPatchDispatchRequest) { r.SchemaVersion = 0 }, func(r *mediav1.BeginMediaPatchDispatchRequest) { r.Fence.ExecutionEpoch++ }, func(r *mediav1.BeginMediaPatchDispatchRequest) { r.ClaimToken = "wrong" }, func(r *mediav1.BeginMediaPatchDispatchRequest) { r.WriterFencingToken++ }, func(r *mediav1.BeginMediaPatchDispatchRequest) { r.PreparationSha256 = nil }} {
		r := proto.Clone(mediaBegin(claim)).(*mediav1.BeginMediaPatchDispatchRequest)
		mutate(r)
		if _, err := c.BeginMediaPatchDispatch(r); err == nil {
			t.Fatal("invalid claim dispatched")
		}
	}
	permit, err := c.BeginMediaPatchDispatch(mediaBegin(claim))
	if err != nil || !permit.Fresh {
		t.Fatal(err)
	}
	replay, err := c.BeginMediaPatchDispatch(mediaBegin(claim))
	if err != nil || replay.Fresh {
		t.Fatal("replayed permit can write", err)
	}
	wrong := mediaBegin(claim)
	wrong.PreparationSha256 = bytes.Repeat([]byte{8}, 32)
	if _, err := c.BeginMediaPatchDispatch(wrong); err == nil {
		t.Fatal("rebound preparation")
	}
	*now = 1100
	if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-two")); !errors.Is(err, ErrMediaEffectUnknown) {
		t.Fatal("expiry released uncertain publication", err)
	}
	if err := c.CancelUndispatchedMediaPatch(claim.Intent.OperationId); !errors.Is(err, ErrMediaEffectUnknown) {
		t.Fatal("cancelled dispatched effect", err)
	}
	pending, err := c.PendingMediaPatch()
	if err != nil || pending.Fence.ExecutionEpoch != 1 || pending.Intent.OperationId != claim.Intent.OperationId {
		t.Fatal(err)
	}
}
func TestMediaSignedHistoricalOutcomeSettlesAfterExpiryAndCannotBeChanged(t *testing.T) {
	c, now, key := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-one"))
	if err != nil {
		t.Fatal(err)
	}
	proof := mediaProof(t, claim, key)
	if _, err := c.RecordMediaPatchOutcome(proof); err == nil {
		t.Fatal("effect without dispatch accepted")
	}
	if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); err != nil {
		t.Fatal(err)
	}
	*now = 1100
	forged := proto.Clone(proof).(*mediav1.MediaPatchOutcome)
	forged.Signature[0] ^= 1
	if _, err := c.RecordMediaPatchOutcome(forged); err == nil {
		t.Fatal("forged outcome accepted")
	}
	if already, err := c.RecordMediaPatchOutcome(proof); err != nil || already {
		t.Fatal(err)
	}
	if already, err := c.RecordMediaPatchOutcome(proof); err != nil || !already {
		t.Fatal("historical receipt not retained", err)
	}
	other := proto.Clone(proof).(*mediav1.MediaPatchOutcome)
	other.AfterSha256[0]++
	signMediaProof(t, other, key)
	if _, err := c.RecordMediaPatchOutcome(other); !errors.Is(err, ErrRevisionConflict) {
		t.Fatal("terminal witness changed", err)
	}
	if pending, err := c.PendingMediaPatch(); err != nil || pending != nil {
		t.Fatal("settled resource retained", err)
	}
	if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-two")); err != nil {
		t.Fatal(err)
	}
	if _, err := c.db.Exec("DELETE FROM control_media_operations WHERE operation_id=?", claim.Intent.OperationId); err == nil {
		t.Fatal("history deleted")
	}
	if _, err := c.db.Exec("UPDATE control_media_operations SET intent=X'00' WHERE operation_id=?", claim.Intent.OperationId); err == nil {
		t.Fatal("immutable intent changed")
	}
}
func TestMediaPreparedRefusalCanSettleWithoutInventingADispatch(t *testing.T) {
	c, _, key := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-one"))
	if err != nil {
		t.Fatal(err)
	}
	proof := mediaProof(t, claim, key)
	proof.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
	proof.HttpStatus = 409
	proof.ErrorCode = "execution_interrupted"
	proof.ErrorMessage = "Interrupted before publication"
	proof.Media = nil
	proof.AfterSha256 = nil
	signMediaProof(t, proof, key)
	if _, err := c.RecordMediaPatchOutcome(proof); err != nil {
		t.Fatal(err)
	}
	if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); err == nil {
		t.Fatal("late permit after proved refusal")
	}
	if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-two")); err != nil {
		t.Fatal(err)
	}
}
