package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// This is an explicit Go-only transport peer, not Rust custody or file-effect
// acceptance. The separate actual-launcher probe remains mandatory.
type mediaWirePeer struct {
	mediav1.UnimplementedMediaMutationWorkerServer
	mu                sync.Mutex
	control           *store.Control
	key               ed25519.PrivateKey
	mode              string
	executed, queried int
	retained          *mediav1.MediaPatchOutcome
}

func (p *mediaWirePeer) ExecutePatch(_ context.Context, r *mediav1.ExecuteMediaPatchRequest) (*mediav1.MediaPatchOutcome, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.executed++
	claim, err := p.control.GetMediaPatchClaim(r.OperationId, r.Fence)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	_, err = p.control.BeginMediaPatchDispatch(&mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1, OperationId: r.OperationId, Fence: r.Fence, ClaimToken: claim.ClaimToken, WriterFencingToken: claim.WriterFencingToken, PreparationSha256: bytes.Repeat([]byte{7}, 32)})
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	if p.mode == "unknown" {
		return nil, status.Error(codes.Unavailable, "isolated response loss without retained evidence")
	}
	value, err := mediamodel.FromJSON(map[string]any{"mediaId": claim.Intent.MediaId, "title": "Changed", "metadata": map[string]any{"retained": true}})
	if err != nil {
		return nil, err
	}
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: r.OperationId, OriginalFence: proto.Clone(r.Fence).(*commonv1.ActionFence), RequestSha256: claim.RequestSha256, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, Media: value.GetObjectValue(), HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{1}, 32), AfterSha256: bytes.Repeat([]byte{2}, 32), FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken}
	if p.mode == "refused" {
		outcome.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
		outcome.Media = nil
		outcome.AfterSha256 = nil
		outcome.HttpStatus = 404
		outcome.ErrorCode = "not_found"
		outcome.ErrorMessage = "Media not found"
	}
	b, err := mediamodel.Encode(outcome)
	if err != nil {
		return nil, err
	}
	outcome.Signature = ed25519.Sign(p.key, append([]byte("deepseek-infra:media-patch-outcome-v1\x00"), b...))
	p.retained = outcome
	if p.mode == "lost" {
		return nil, status.Error(codes.Unavailable, "isolated reply lost after retention")
	}
	return proto.Clone(outcome).(*mediav1.MediaPatchOutcome), nil
}
func (p *mediaWirePeer) QueryPatch(_ context.Context, r *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.queried++
	if p.retained == nil {
		return &mediav1.QueryMediaPatchResponse{Found: false}, nil
	}
	if r.OperationId != p.retained.OperationId || !proto.Equal(r.OriginalFence, p.retained.OriginalFence) {
		return nil, status.Error(codes.FailedPrecondition, "query rebound original operation")
	}
	return &mediav1.QueryMediaPatchResponse{Found: true, Outcome: proto.Clone(p.retained).(*mediav1.MediaPatchOutcome)}, nil
}
func mediaWireFixture(t *testing.T, mode string) (*store.Control, *mediaWirePeer, *httptest.Server) {
	t.Helper()
	public, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	control, err := store.OpenControl(store.OpenOptions{Path: filepath.Join(root, "control"), Owner: "media-wire", MediaOutcomeSignerPublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "media-wire-fleet", Environment: "isolated", LeaseSeconds: 3600})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { control.Close() })
	peer := &mediaWirePeer{control: control, key: key, mode: mode}
	if mode == "forged" {
		_, peer.key, err = ed25519.GenerateKey(rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
	}
	bearer := strings.Repeat("t", 32)
	rpc := grpc.NewServer(grpc.UnaryInterceptor(func(ctx context.Context, r any, _ *grpc.UnaryServerInfo, next grpc.UnaryHandler) (any, error) {
		md, _ := metadata.FromIncomingContext(ctx)
		values := md.Get("authorization")
		if len(values) != 1 || values[0] != "Bearer "+bearer {
			return nil, status.Error(codes.Unauthenticated, "test peer requires bearer")
		}
		return next(ctx, r)
	}))
	mediav1.RegisterMediaMutationWorkerServer(rpc, peer)
	t.Cleanup(rpc.Stop)
	tlsServer := httptest.NewUnstartedServer(rpc)
	tlsServer.EnableHTTP2 = true
	tlsServer.StartTLS()
	t.Cleanup(tlsServer.Close)
	certificate := tlsServer.Certificate()
	if certificate == nil || len(certificate.DNSNames) == 0 {
		t.Fatal("TLS fixture has no server identity")
	}
	ca := filepath.Join(root, "tls-root.pem")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certificate.Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(worker.EnvWorkerTarget, strings.TrimPrefix(tlsServer.URL, "https://"))
	t.Setenv(worker.EnvWorkerTLSCAFile, ca)
	t.Setenv(worker.EnvWorkerTLSServerName, certificate.DNSNames[0])
	t.Setenv(worker.EnvWorkerServiceBearer, bearer)
	t.Setenv(worker.EnvWorkerServiceBearerExpires, time.Now().UTC().Add(10*time.Minute).Format("2006-01-02T15:04:05Z"))
	t.Setenv("AUTH_DISABLED", "0")
	t.Setenv("AUTH_TOKEN", strings.Repeat("m", 32))
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	mux := http.NewServeMux()
	RegisterPublicView(mux, control, PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo})
	server := httptest.NewServer(mux)
	server.Client().Timeout = 10 * time.Second
	t.Cleanup(server.Close)
	return control, peer, server
}
func mediaWirePatch(t *testing.T, server *httptest.Server) (int, map[string]any, string) {
	t.Helper()
	r, err := http.NewRequest(http.MethodPatch, server.URL+"/api/media/media_one", strings.NewReader(`{"title":"Changed"}`))
	if err != nil {
		t.Fatal(err)
	}
	r.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
	response, err := server.Client().Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var body map[string]any
	if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, body, response.Header.Get("X-DeepSeek-Operation-Id")
}
func TestMediaPublicTLSRoundTripSettlesOnlyTheBoundSignedOutcome(t *testing.T) {
	control, peer, server := mediaWireFixture(t, "success")
	code, body, id := mediaWirePatch(t, server)
	if code != 200 || body["ok"] != true || id == "" {
		t.Fatalf("response %d %#v", code, body)
	}
	media, ok := body["media"].(map[string]any)
	if !ok || media["mediaId"] != "media_one" || media["title"] != "Changed" {
		t.Fatalf("media %#v", media)
	}
	claim, err := control.GetMediaPatchClaim(id, &commonv1.ActionFence{ActionId: id, ExecutionEpoch: 1})
	if err != nil || claim.State != "APPLIED" {
		t.Fatal("no durable settlement", err)
	}
	peer.mu.Lock()
	defer peer.mu.Unlock()
	if peer.executed != 1 || peer.queried != 0 {
		t.Fatal("unexpected dispatch/query counts", peer.executed, peer.queried)
	}
}
func TestMediaPublicLostTLSReplyQueriesTheOriginalWithoutRedispatch(t *testing.T) {
	control, peer, server := mediaWireFixture(t, "lost")
	code, body, id := mediaWirePatch(t, server)
	if code != 200 || body["ok"] != true {
		t.Fatalf("response %d %#v", code, body)
	}
	pending, err := control.PendingMediaPatch()
	if err != nil || pending != nil {
		t.Fatal("original signed result was not settled", err)
	}
	peer.mu.Lock()
	defer peer.mu.Unlock()
	if peer.executed != 1 || peer.queried != 1 || peer.retained.OperationId != id {
		t.Fatal("lost reply was rebound or redispatched")
	}
}
func TestMediaPublicUnresolvedTLSReplyBlocksAnotherOperationDespiteAbsence(t *testing.T) {
	control, peer, server := mediaWireFixture(t, "unknown")
	first, body, id := mediaWirePatch(t, server)
	if first != 503 || body["code"] != "EFFECT_UNKNOWN" || id == "" {
		t.Fatalf("response %d %#v", first, body)
	}
	second, body, original := mediaWirePatch(t, server)
	if second != 503 || body["code"] != "EFFECT_UNKNOWN" || original != id {
		t.Fatal("uncertainty did not preserve original identity")
	}
	pending, err := control.PendingMediaPatch()
	if err != nil || pending == nil || pending.Intent.OperationId != id || pending.State != "DISPATCHED" {
		t.Fatal("uncertainty reservation released", err)
	}
	peer.mu.Lock()
	defer peer.mu.Unlock()
	if peer.executed != 1 || peer.queried != 2 {
		t.Fatal("absence caused another effect", peer.executed, peer.queried)
	}
}
func TestMediaPublicForgedTLSResultCannotSettleTheClaim(t *testing.T) {
	control, peer, server := mediaWireFixture(t, "forged")
	code, body, id := mediaWirePatch(t, server)
	if code != 503 || body["code"] != "EFFECT_UNKNOWN" {
		t.Fatalf("forged result accepted %d %#v", code, body)
	}
	claim, err := control.GetMediaPatchClaim(id, &commonv1.ActionFence{ActionId: id, ExecutionEpoch: 1})
	if err != nil || claim.State != "DISPATCHED" {
		t.Fatal("forged witness settled operation", err)
	}
	peer.mu.Lock()
	defer peer.mu.Unlock()
	if peer.executed != 1 || peer.queried != 1 {
		t.Fatal("forged result caused redispatch")
	}
}
func TestMediaPublicTLSRefusalRetainsTheOriginalHTTPErrorAndReleasesAdmission(t *testing.T) {
	control, _, server := mediaWireFixture(t, "refused")
	code, body, id := mediaWirePatch(t, server)
	if code != 404 || body["code"] != "not_found" || body["error"] != "Media not found" {
		t.Fatalf("refusal %d %#v", code, body)
	}
	claim, err := control.GetMediaPatchClaim(id, &commonv1.ActionFence{ActionId: id, ExecutionEpoch: 1})
	if err != nil || claim.State != "REFUSED" {
		t.Fatal("refusal not durably settled", err)
	}
	if pending, err := control.PendingMediaPatch(); err != nil || pending != nil {
		t.Fatal("proved refusal retained admission", err)
	}
}
