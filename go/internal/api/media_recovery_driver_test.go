package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

type mediaDriverFixture struct {
	control *store.Control
	options store.OpenOptions
	claim   *mediav1.MediaPatchClaim
	key     ed25519.PrivateKey // Isolated test signer; no production key enters Go.
}

func mediaDriverStore(t *testing.T, dispatched, expired bool) *mediaDriverFixture {
	t.Helper()
	public, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	if expired {
		now -= 120
	}
	options := store.OpenOptions{Path: filepath.Join(t.TempDir(), "control"), Owner: "media-driver", LeaseSeconds: 3600,
		Now: func() int64 { return now }, MediaOutcomeSignerPublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "driver-fleet", Environment: "isolated"}
	control, err := store.OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	f := &mediaDriverFixture{control: control, options: options, key: key}
	t.Cleanup(func() { _ = f.control.Close() })
	f.claim, err = control.AdmitMediaPatch(mediaRecoveryIntent("media-patch-driver"))
	if err != nil {
		t.Fatal(err)
	}
	if dispatched {
		_, err = control.BeginMediaPatchDispatch(&mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1,
			OperationId: f.claim.Intent.OperationId, Fence: f.claim.Fence, ClaimToken: f.claim.ClaimToken,
			WriterFencingToken: f.claim.WriterFencingToken, PreparationSha256: bytes.Repeat([]byte{7}, 32)})
		if err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func (f *mediaDriverFixture) sign(t *testing.T, outcome *mediav1.MediaPatchOutcome) {
	t.Helper()
	outcome.Signature = nil
	encoded, err := mediamodel.Encode(outcome)
	if err != nil {
		t.Fatal(err)
	}
	outcome.Signature = ed25519.Sign(f.key, append([]byte("deepseek-infra:media-patch-outcome-v1\x00"), encoded...))
}

func (f *mediaDriverFixture) proof(t *testing.T, refused bool) *mediav1.MediaPatchOutcome {
	t.Helper()
	value, err := mediamodel.FromJSON(map[string]any{"mediaId": f.claim.Intent.MediaId, "title": "changed"})
	if err != nil {
		t.Fatal(err)
	}
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: f.claim.Intent.OperationId,
		OriginalFence: proto.Clone(f.claim.Fence).(*commonv1.ActionFence), RequestSha256: append([]byte(nil), f.claim.RequestSha256...),
		State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, Media: value.GetObjectValue(), HttpStatus: 200,
		BeforeSha256: bytes.Repeat([]byte{1}, 32), AfterSha256: bytes.Repeat([]byte{2}, 32), FleetId: f.claim.FleetId,
		Environment: f.claim.Environment, WriterFencingToken: f.claim.WriterFencingToken}
	if refused {
		outcome.State, outcome.Media, outcome.AfterSha256 = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED, nil, nil
		outcome.HttpStatus, outcome.ErrorCode, outcome.ErrorMessage = 409, "execution_interrupted", "Interrupted before publication"
	}
	f.sign(t, outcome)
	return outcome
}

// Deliberately query-only: implementing ExecuteMediaPatch is not required or allowed.
type mediaDriverPeer struct {
	query  func(context.Context, *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error)
	closed int
}

func (peer *mediaDriverPeer) QueryMediaPatch(ctx context.Context, input *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error) {
	return peer.query(ctx, input)
}
func (peer *mediaDriverPeer) Close() error { peer.closed++; return nil }

func mediaDriverReply(t *testing.T, f *mediaDriverFixture, response *mediav1.QueryMediaPatchResponse, queryErr error) *mediaDriverPeer {
	t.Helper()
	return &mediaDriverPeer{query: func(ctx context.Context, request *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error) {
		if request.SchemaVersion != 1 || request.OperationId != f.claim.Intent.OperationId || !proto.Equal(request.OriginalFence, f.claim.Fence) {
			t.Error("recovery rebound the original operation/fence")
		}
		if deadline, ok := ctx.Deadline(); !ok || time.Until(deadline) > 5*time.Second {
			t.Error("query lacks its bounded deadline")
		}
		return response, queryErr
	}}
}

func TestMediaRecoveryDriverSuccessorSettlesOriginalWithoutRedispatch(t *testing.T) {
	f := mediaDriverStore(t, true, false)
	proof := f.proof(t, false)
	if err := f.control.Close(); err != nil {
		t.Fatal(err)
	}
	f.options.Owner = "media-successor"
	var err error
	f.control, err = store.OpenControl(f.options)
	if err != nil || f.control.Writer().FencingToken <= f.claim.WriterFencingToken {
		t.Fatal("successor writer unavailable", err)
	}
	peer := mediaDriverReply(t, f, &mediav1.QueryMediaPatchResponse{Found: true, Outcome: proof}, nil)
	calls := 0
	dial := func() (mediaRecoveryWorker, error) { calls++; return peer, nil }
	reconcilePendingMediaPatch(context.Background(), f.control, time.Now().Unix(), dial)
	reconcilePendingMediaPatch(context.Background(), f.control, time.Now().Unix(), dial)
	retained, err := f.control.GetMediaPatchClaim(f.claim.Intent.OperationId, f.claim.Fence)
	if err != nil || retained.State != "APPLIED" || calls != 1 || peer.closed != 1 {
		t.Fatal("original did not settle exactly once", err)
	}
	retained.State = f.claim.State
	if !proto.Equal(retained, f.claim) {
		t.Fatal("historical claim identity changed during successor reconciliation")
	}
}

func TestMediaRecoveryDriverSignedRefusalSettlesWithoutDispatch(t *testing.T) {
	f := mediaDriverStore(t, false, true)
	peer := mediaDriverReply(t, f, &mediav1.QueryMediaPatchResponse{Found: true, Outcome: f.proof(t, true)}, nil)
	reconcilePendingMediaPatch(context.Background(), f.control, time.Now().Unix(), func() (mediaRecoveryWorker, error) { return peer, nil })
	claim, err := f.control.GetMediaPatchClaim(f.claim.Intent.OperationId, f.claim.Fence)
	if err != nil || claim.State != "REFUSED" || peer.closed != 1 {
		t.Fatal("signed original refusal was not retained", err)
	}
}

func TestMediaRecoveryDriverAbsenceAndInvalidEvidenceRetainTheBarrier(t *testing.T) {
	for _, kind := range []string{"absent", "nil", "no-outcome", "query-error", "dial-error", "unsigned", "wrong-epoch", "wrong-request", "unknown"} {
		t.Run(kind, func(t *testing.T) {
			f := mediaDriverStore(t, true, true)
			before, err := f.control.PendingMediaPatch()
			if err != nil {
				t.Fatal(err)
			}
			proof := f.proof(t, false)
			reply := &mediav1.QueryMediaPatchResponse{Found: true, Outcome: proof}
			var queryErr error
			switch kind {
			case "absent":
				reply.Found, reply.Outcome = false, nil
			case "nil":
				reply = nil
			case "no-outcome":
				reply.Outcome = nil
			case "query-error":
				queryErr = errors.New("transport interrupted")
			case "unsigned":
				proof.Signature = nil
			case "wrong-epoch":
				proof.OriginalFence.ExecutionEpoch++
				f.sign(t, proof)
			case "wrong-request":
				proof.RequestSha256[0] ^= 1
				f.sign(t, proof)
			case "unknown":
				proof.State, proof.Media, proof.AfterSha256 = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_UNKNOWN, nil, nil
				f.sign(t, proof)
			}
			peer := mediaDriverReply(t, f, reply, queryErr)
			dial := func() (mediaRecoveryWorker, error) {
				if kind == "dial-error" {
					return nil, errors.New("secured peer unavailable")
				}
				return peer, nil
			}
			reconcilePendingMediaPatch(context.Background(), f.control, time.Now().Unix(), dial)
			after, err := f.control.PendingMediaPatch()
			if err != nil || !proto.Equal(before, after) {
				t.Fatal("uncertainty changed the original pending claim", err)
			}
			if _, err := f.control.AdmitMediaPatch(mediaRecoveryIntent("media-patch-retry")); !errors.Is(err, store.ErrMediaEffectUnknown) {
				t.Fatal("uncertainty released admission", err)
			}
			if kind != "dial-error" && peer.closed != 1 {
				t.Fatal("query client was not closed")
			}
		})
	}
}

func TestMediaRecoveryDriverSkipsLiveClaimsAndRetiresOnlyExpiredUndispatchedAbsence(t *testing.T) {
	f := mediaDriverStore(t, false, true)
	queries := 0
	peer := &mediaDriverPeer{query: func(context.Context, *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error) {
		queries++
		return &mediav1.QueryMediaPatchResponse{Found: false}, nil
	}}
	dial := func() (mediaRecoveryWorker, error) { return peer, nil }
	reconcilePendingMediaPatch(context.Background(), f.control, f.claim.LeaseUntilUnix-1, dial)
	if queries != 0 {
		t.Fatal("live same-writer claim was queried")
	}
	reconcilePendingMediaPatch(context.Background(), f.control, time.Now().Unix(), dial)
	claim, err := f.control.GetMediaPatchClaim(f.claim.Intent.OperationId, f.claim.Fence)
	if err != nil || claim.State != "CANCELLED" || queries != 1 {
		t.Fatal("durable expired pre-dispatch absence was not retired", err)
	}
	if _, err := f.control.BeginMediaPatchDispatch(&mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1, OperationId: f.claim.Intent.OperationId, Fence: f.claim.Fence, ClaimToken: f.claim.ClaimToken, WriterFencingToken: f.claim.WriterFencingToken, PreparationSha256: bytes.Repeat([]byte{7}, 32)}); err == nil {
		t.Fatal("late dispatch after conclusive cancellation")
	}
}

func TestMediaRecoveryDriverCancellationDrainsTheQueryAndClosesItsClient(t *testing.T) {
	f := mediaDriverStore(t, true, true)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	entered, done := make(chan struct{}), make(chan struct{})
	peer := &mediaDriverPeer{query: func(ctx context.Context, _ *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error) {
		close(entered)
		<-ctx.Done()
		return nil, ctx.Err()
	}}
	go func() {
		runMediaPatchRecovery(ctx, f.control, make(chan time.Time), func() (mediaRecoveryWorker, error) { return peer, nil })
		close(done)
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("query was not entered")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("query did not drain on cancellation")
	}
	if peer.closed != 1 {
		t.Fatal("cancelled peer was not closed")
	}
}

func TestMediaRecoveryDriverEmptyAndClosedStoresDoNotDialAndTicksCancel(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "empty-media"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	dial := func() (mediaRecoveryWorker, error) {
		t.Error("empty/closed store dialed worker")
		return nil, errors.New("unexpected dial")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done, ticks := make(chan struct{}), make(chan time.Time)
	go func() { runMediaPatchRecovery(ctx, control, ticks, dial); close(done) }()
	select {
	case ticks <- time.Now():
	case <-time.After(5 * time.Second):
		t.Fatal("empty loop did not reach its ticker")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("empty loop did not stop")
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reconcilePendingMediaPatch(context.Background(), control, time.Now().Unix(), dial)
	reconcilePendingMediaPatch(ctx, nil, time.Now().Unix(), dial)
}

func TestMediaRecoveryDriverStartsOnlyInTheNativeAuthoritativeLifecycle(t *testing.T) {
	f := mediaDriverStore(t, true, false)
	auth := ControlPlaneHealth{Mode: controlv1.RuntimeMode_RUNTIME_MODE_AUTHORITATIVE, MutationAuthority: config.MutationAuthorityGo}
	for _, item := range []struct {
		name, runtime string
		control       *store.Control
		health        ControlPlaneHealth
	}{
		{"nil", "python_disabled", nil, auth}, {"shadow", "python_disabled", f.control, ShadowHealth()},
		{"authority", "python_disabled", f.control, ControlPlaneHealth{Mode: auth.Mode}}, {"python", "python", f.control, auth},
		{"native-cancelled", "python_disabled", f.control, auth},
	} {
		t.Run(item.name, func(t *testing.T) {
			t.Setenv("DEEPSEEK_RUNTIME_MODE", item.runtime)
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			select {
			case <-StartMediaPatchRecovery(ctx, item.control, item.health):
			case <-time.After(5 * time.Second):
				t.Fatal("inactive/cancelled driver did not close")
			}
		})
	}
	t.Setenv("DEEPSEEK_WORKER_SERVICE_BEARER", "")
	if _, err := dialMediaRecoveryWorker(); err == nil {
		t.Fatal("recovery bypassed the configured secured worker channel")
	}
}
