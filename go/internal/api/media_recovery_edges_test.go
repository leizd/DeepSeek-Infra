package api

import (
	"bytes"
	"context"
	"errors"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/protobuf/proto"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Explicit response fixture for recovery decisions; it performs no data effect.
type mediaRecoveryReplyFixture struct {
	response          *mediav1.QueryMediaPatchResponse
	failure           error
	query             *mediav1.QueryMediaPatchRequest
	calls, executions int
}

func (f *mediaRecoveryReplyFixture) ExecuteMediaPatch(context.Context, *mediav1.ExecuteMediaPatchRequest) (*mediav1.MediaPatchOutcome, error) {
	f.executions++
	return nil, errors.New("recovery must not execute")
}
func (f *mediaRecoveryReplyFixture) QueryMediaPatch(_ context.Context, r *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error) {
	f.calls++
	f.query = proto.Clone(r).(*mediav1.QueryMediaPatchRequest)
	return f.response, f.failure
}
func mediaRecoveryIntent(id string) *mediav1.MediaPatchIntent {
	return &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: id, MediaId: "media_one", Patch: &mediav1.MediaPatch{Title: &mediav1.MediaValue{Kind: &mediav1.MediaValue_TextValue{TextValue: "changed"}}}}
}

func TestMediaRecoveryKeepsQueryFailuresAndIncompleteRepliesUnknown(t *testing.T) {
	c, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-recovery"})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	claim, err := c.AdmitMediaPatch(mediaRecoveryIntent("media-patch-recovery"))
	if err != nil {
		t.Fatal(err)
	}
	for name, f := range map[string]*mediaRecoveryReplyFixture{
		"transport": {failure: errors.New("isolated query failure")}, "nil-response": {}, "absent-live-operation": {response: &mediav1.QueryMediaPatchResponse{Found: false}}, "missing-proof": {response: &mediav1.QueryMediaPatchResponse{Found: true}}, "invalid-proof": {response: &mediav1.QueryMediaPatchResponse{Found: true, Outcome: &mediav1.MediaPatchOutcome{OperationId: claim.Intent.OperationId}}},
	} {
		t.Run(name, func(t *testing.T) {
			if result, settled := recoverMediaPatch(context.Background(), c, f, claim); result != nil || settled {
				t.Fatal("inconclusive query settled")
			}
			if f.calls != 1 || f.executions != 0 || f.query.OperationId != claim.Intent.OperationId || !proto.Equal(f.query.OriginalFence, claim.Fence) {
				t.Fatal("recovery rebound or repeated the original")
			}
			pending, err := c.PendingMediaPatch()
			if err != nil || pending == nil || pending.State != "CLAIMED" {
				t.Fatal("uncertainty released the reservation", err)
			}
		})
	}
	for _, invalid := range []*mediav1.MediaPatchClaim{nil, {}, {Intent: claim.Intent}} {
		f := &mediaRecoveryReplyFixture{}
		if _, settled := recoverMediaPatch(context.Background(), c, f, invalid); settled || f.calls != 0 {
			t.Fatal("unbound query issued")
		}
	}
}
func TestMediaRecoveryCancelsOnlyDurableUndispatchedAbsence(t *testing.T) {
	c, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-expired-fixture", Now: func() int64 { return 1000 }, LeaseSeconds: 3600})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	claim, err := c.AdmitMediaPatch(mediaRecoveryIntent("media-patch-expired"))
	if err != nil {
		t.Fatal(err)
	}
	if claim.LeaseUntilUnix >= time.Now().Unix() {
		t.Fatal("fixed offline lease fixture is not expired")
	}
	f := &mediaRecoveryReplyFixture{response: &mediav1.QueryMediaPatchResponse{Found: false}}
	result, settled := recoverMediaPatch(context.Background(), c, f, claim)
	if result != nil || !settled || f.calls != 1 || f.executions != 0 {
		t.Fatal("undispatched cancellation failed")
	}
	retained, err := c.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence)
	if err != nil || retained.State != "CANCELLED" {
		t.Fatal("cancellation not retained", err)
	}
	next, err := c.AdmitMediaPatch(mediaRecoveryIntent("media-patch-dispatched"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = c.BeginMediaPatchDispatch(&mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1, OperationId: next.Intent.OperationId, Fence: next.Fence, ClaimToken: next.ClaimToken, WriterFencingToken: next.WriterFencingToken, PreparationSha256: bytes.Repeat([]byte{7}, 32)}); err != nil {
		t.Fatal(err)
	}
	next, err = c.GetMediaPatchClaim(next.Intent.OperationId, next.Fence)
	if err != nil {
		t.Fatal(err)
	}
	if _, settled := recoverMediaPatch(context.Background(), c, f, next); settled {
		t.Fatal("absence legalized a dispatched retry")
	}
	if pending, err := c.PendingMediaPatch(); err != nil || pending == nil || pending.Intent.OperationId != next.Intent.OperationId {
		t.Fatal("dispatched reservation lost", err)
	}
}
func TestMediaPublicIngressKeepsHostPathDepthAndFieldPresenceChecks(t *testing.T) {
	c, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-ingress"})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	t.Setenv("AUTH_DISABLED", "0")
	t.Setenv("AUTH_ALLOWED_HOSTS", "")
	t.Setenv("HOST", "127.0.0.1")
	t.Setenv("AUTH_TOKEN", strings.Repeat("m", 32))
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	for _, name := range []string{worker.EnvWorkerTarget, worker.EnvWorkerTLSCAFile, worker.EnvWorkerTLSServerName, worker.EnvWorkerServiceBearer, worker.EnvWorkerServiceBearerExpires} {
		t.Setenv(name, "")
	}
	for _, test := range []struct {
		name, path, host, body string
		status                 int
		code                   string
	}{
		{"host", "/api/media/media_one", "untrusted.example", `{"title":"x"}`, 403, "forbidden"},
		{"empty-id", "/api/media/", "", `{"title":"x"}`, 501, "GO_API_NOT_IMPLEMENTED"},
		{"nested-path", "/api/media/media_one/child", "", `{"title":"x"}`, 501, "GO_API_NOT_IMPLEMENTED"},
		{"depth", "/api/media/media_one", "", `{"metadata":` + strings.Repeat("[", 140) + `null` + strings.Repeat("]", 140) + `}`, 400, "invalid_payload"},
		{"present-null-fields", "/api/media/media_one", "", `{"projectId":null,"metadata":null}`, 503, "MEDIA_WORKER_UNAVAILABLE"},
	} {
		t.Run(test.name, func(t *testing.T) {
			r := httptest.NewRequest("PATCH", "http://127.0.0.1"+test.path, strings.NewReader(test.body))
			r.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
			if test.host != "" {
				r.Host = test.host
			}
			w := httptest.NewRecorder()
			mediaPatch(w, r, c, PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo})
			if w.Code != test.status || !strings.Contains(w.Body.String(), test.code) {
				t.Fatalf("refusal %d %s", w.Code, w.Body.String())
			}
		})
	}
}
func TestMediaPublicClosedControlAndMalformedReplyRetainUncertainty(t *testing.T) {
	c, peer, server := mediaWireFixture(t, "success")
	if err := c.Close(); err != nil {
		t.Fatal(err)
	}
	code, body, id := mediaWirePatch(t, server)
	if code != 503 || body["code"] != "EFFECT_UNKNOWN" || id != "" {
		t.Fatalf("closed control minted an operation: %d %#v %q", code, body, id)
	}
	peer.mu.Lock()
	calls := peer.executed + peer.queried
	peer.mu.Unlock()
	if calls != 0 {
		t.Fatal("closed control reached worker")
	}
	w := httptest.NewRecorder()
	r := httptest.NewRequest("PATCH", "http://127.0.0.1/api/media/media_one", nil)
	writeMediaOutcome(w, r, &mediav1.MediaPatchOutcome{OperationId: "media-patch-malformed", Media: &mediav1.MediaObject{Members: []*mediav1.MediaMember{{Key: "invalid", Value: nil}}}})
	if w.Code != 503 || w.Header().Get("X-DeepSeek-Operation-Id") != "media-patch-malformed" || !strings.Contains(w.Body.String(), "EFFECT_UNKNOWN") {
		t.Fatal("malformed internal reply became success", w.Code, w.Body.String())
	}
}
