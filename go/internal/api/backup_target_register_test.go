package api

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// This TLS peer proves HTTP admission and Go persistence only. Its fixture
// signatures do not qualify the physical Rust filesystem or custody process.
type publicTargetPeer struct {
	actionv1.UnimplementedBackupTargetInspectionServer
	mode    string
	binding store.BackupTargetObservationContext
	calls   atomic.Int64
}

func (p *publicTargetPeer) Inspect(ctx context.Context, r *actionv1.InspectFilesystemBackupTargetRequest) (*actionv1.FilesystemBackupTargetInspectionReceipt, error) {
	p.calls.Add(1)
	md, _ := metadata.FromIncomingContext(ctx)
	if values := md.Get("authorization"); len(values) != 1 || values[0] != "Bearer "+strings.Repeat("t", 32) {
		return nil, status.Error(codes.Unauthenticated, "isolated controller credential required")
	}
	if !r.RequireFullContainment {
		return nil, status.Error(codes.InvalidArgument, "complete containment was not requested")
	}
	if p.mode == "denied" {
		return nil, status.Error(codes.InvalidArgument, "Unsafe backup target: isolated containment refusal")
	}
	marker := []byte(`{"targetId":"target_public","targetNonce":"retained","createdAt":"2000-01-01T00:00:00Z"}`)
	digest := sha256.Sum256(marker)
	o := &actionv1.FilesystemBackupTargetObservation{SchemaVersion: 1, Fence: r.Fence, OperationId: r.OperationId,
		WriterFencingToken: r.WriterFencingToken, RequestedPath: r.Path, CanonicalPath: filepath.Clean(r.Path),
		MarkerPresent: true, MarkerJson: marker, MarkerSha256: digest[:], ObservedAtUnix: time.Now().Unix(),
		FleetId: p.binding.FleetID, Environment: p.binding.Environment, FullContainmentChecked: p.mode != "old-worker"}
	if p.mode == "absent" {
		o.MarkerPresent = false
		o.MarkerJson = nil
		o.MarkerSha256 = nil
	}
	wire, _ := proto.MarshalOptions{Deterministic: true}.Marshal(o)
	receipt := &actionv1.FilesystemBackupTargetInspectionReceipt{SchemaVersion: 1, Observation: o, SignerPublicKey: p.binding.PublicKey,
		Signature: ed25519.Sign(promotionRoutePrivate, append([]byte("deepseek-infra:backup-target-inspection-v1\x00"), wire...))}
	if p.mode == "forged" {
		receipt.Signature[0] ^= 1
	}
	return receipt, nil
}
func (p *publicTargetPeer) ProbeLocation(_ context.Context, r *actionv1.ProbeFilesystemBackupTargetLocationRequest) (*actionv1.FilesystemBackupTargetLocationReceipt, error) {
	p.calls.Add(1)
	o := &actionv1.FilesystemBackupTargetLocationObservation{SchemaVersion: 1, Fence: r.Fence, OperationId: r.OperationId, WriterFencingToken: r.WriterFencingToken,
		RequestedPath: r.Path, ObservedAtUnix: time.Now().Unix(), FleetId: p.binding.FleetID, Environment: p.binding.Environment,
		State: actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING}
	if r.ResolvePath {
		o.State = actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_UNSPECIFIED
		o.CanonicalPath = filepath.Clean(r.Path)
	}
	wire, _ := proto.MarshalOptions{Deterministic: true}.Marshal(o)
	return &actionv1.FilesystemBackupTargetLocationReceipt{SchemaVersion: 1, Observation: o, SignerPublicKey: p.binding.PublicKey,
		Signature: ed25519.Sign(promotionRoutePrivate, append([]byte("deepseek-infra:backup-target-location-v1\x00"), wire...))}, nil
}

func publicTargetTLSFixture(t *testing.T, mode string) (*store.Control, *publicTargetPeer, *httptest.Server, *httptest.Server) {
	t.Helper()
	control := targetPublicFixtureStoreClock(t, true, true, func() int64 { return time.Now().Unix() })
	peer := &publicTargetPeer{mode: mode, binding: control.BackupTargetObservationBinding()}
	rpc := grpc.NewServer()
	actionv1.RegisterBackupTargetInspectionServer(rpc, peer)
	t.Cleanup(rpc.Stop)
	tlsServer := httptest.NewUnstartedServer(rpc)
	tlsServer.EnableHTTP2 = true
	tlsServer.StartTLS()
	t.Cleanup(tlsServer.Close)
	cert := tlsServer.Certificate()
	if cert == nil || len(cert.DNSNames) == 0 {
		t.Fatal("test TLS identity absent")
	}
	ca := filepath.Join(t.TempDir(), "peer-ca.pem")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(worker.EnvWorkerTarget, strings.TrimPrefix(tlsServer.URL, "https://"))
	t.Setenv(worker.EnvWorkerTLSCAFile, ca)
	t.Setenv(worker.EnvWorkerTLSServerName, cert.DNSNames[0])
	t.Setenv(worker.EnvWorkerServiceBearer, strings.Repeat("t", 32))
	t.Setenv(worker.EnvWorkerServiceBearerExpires, time.Now().Add(10*time.Minute).UTC().Format("2006-01-02T15:04:05Z"))
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "public-target-test-token")
	mux := http.NewServeMux()
	RegisterPublicView(mux, control, PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo})
	server := httptest.NewServer(mux)
	server.Client().Timeout = 10 * time.Second
	t.Cleanup(server.Close)
	return control, peer, server, tlsServer
}
func publicTargetHTTPRequest(t *testing.T, server *httptest.Server, method, path, body, operation string) (int, []byte, string) {
	t.Helper()
	request, err := http.NewRequest(method, server.URL+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer public-target-test-token")
	if operation != "" {
		request.Header.Set("X-DeepSeek-Operation-Id", operation)
	}
	response, err := server.Client().Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !json.Valid(raw) {
		t.Fatal("public response is not JSON", string(raw))
	}
	return response.StatusCode, raw, response.Header.Get("X-DeepSeek-Operation-Id")
}

func TestTargetRegistrationPublicTCPPersistsProjectsAndRecoversWithoutAnotherRPC(t *testing.T) {
	control, peer, server, tlsServer := publicTargetTLSFixture(t, "normal")
	path := filepath.Join(t.TempDir(), "existing-directory")
	request, _ := json.Marshal(map[string]any{"path": path, "priority": "١_٢", "storageCostPerGiBMonth": true})
	code, body, operation := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(request), "")
	if code != 200 || !strings.HasPrefix(operation, store.PublicTargetOperationPrefix) {
		t.Fatal("legal public registration failed", code, string(body))
	}
	if strings.Contains(string(body), "_nativeTargetRegistrationV1") || !strings.Contains(string(body), `"priority":12`) {
		t.Fatal("projection leaked evidence or changed retained metadata", string(body))
	}
	record, found, err := control.Get("target", "target_public")
	if err != nil || !found || !strings.Contains(string(record.Payload), "_nativeTargetRegistrationV1") {
		t.Fatal("public response was not durable", err)
	}
	code, listed, _ := publicTargetHTTPRequest(t, server, http.MethodGet, "/api/workspace/backup-targets", "", "")
	if code != 200 || strings.Contains(string(listed), "_nativeTargetRegistrationV1") {
		t.Fatal("public list leaked evidence", code, string(listed))
	}
	before := peer.calls.Load()
	tlsServer.Close()
	code, replayed, replayOperation := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(request), operation)
	if code != 200 || string(replayed) != string(body) || replayOperation != operation || peer.calls.Load() != before {
		t.Fatal("replay contacted unavailable worker or replaced original document", code, string(replayed))
	}
	code, _, _ = publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", `{"path":"changed"}`, operation)
	if code != 409 {
		t.Fatal("replay accepted different request bytes", code)
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 1 {
		t.Fatal("public replay duplicated a write", count, err)
	}
}

func TestTargetRegistrationPublicTCPRejectsAbsentForgedDowngradedAndDeniedReads(t *testing.T) {
	for _, mode := range []string{"absent", "forged", "old-worker", "denied"} {
		t.Run(mode, func(t *testing.T) {
			control, _, server, _ := publicTargetTLSFixture(t, mode)
			request, _ := json.Marshal(map[string]any{"path": filepath.Join(t.TempDir(), "target")})
			code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(request), "")
			expected := 503
			if mode == "denied" {
				expected = 400
			}
			if code != expected {
				t.Fatal("unsafe public read was admitted", mode, code, string(body))
			}
			if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
				t.Fatal("refusal wrote registry", count, err)
			}
		})
	}
}

func TestTargetRegistrationPublicTCPConcurrentIdenticalRequestsCommitOnce(t *testing.T) {
	control, _, server, _ := publicTargetTLSFixture(t, "normal")
	raw, _ := json.Marshal(map[string]any{"path": filepath.Join(t.TempDir(), "target")})
	operation := store.PublicTargetOperationPrefix + "concurrent"
	var wait sync.WaitGroup
	replies := make(chan string, 2)
	for range 2 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(raw), operation)
			if code != 200 {
				t.Errorf("concurrent legal request failed: %d %s", code, body)
			}
			replies <- string(body)
		}()
	}
	wait.Wait()
	close(replies)
	first := ""
	for reply := range replies {
		if first != "" && reply != first {
			t.Fatal("concurrent callers received different committed documents")
		}
		first = reply
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 1 {
		t.Fatal("concurrent request committed twice", count, err)
	}
}

func TestTargetRegistrationPublicAdmissionRefusesAuthorityAuthenticationAndInvalidInputs(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "public-target-test-token")
	authoritative := PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo}
	for _, row := range []struct {
		token, host string
		view        PublicView
		code        int
	}{
		{"", "127.0.0.1", authoritative, 401}, {"public-target-test-token", "hostile.invalid", authoritative, 403},
		{"public-target-test-token", "127.0.0.1", PublicView{}, 503},
	} {
		request := httptest.NewRequest(http.MethodPost, "/api/workspace/backup-targets", strings.NewReader(`{}`))
		request.Host = row.host
		if row.token != "" {
			request.Header.Set("Authorization", "Bearer "+row.token)
		}
		response := httptest.NewRecorder()
		backupTargetRegister(response, request, nil, row.view)
		if response.Code != row.code {
			t.Fatal("public admission changed", response.Code, row.code, response.Body.String())
		}
	}
	control, peer, server, _ := publicTargetTLSFixture(t, "normal")
	for _, row := range []struct {
		body, operation string
		code            int
	}{
		{"", "", 400}, {"[]", "", 400}, {`{"priority":"bad"}`, "", 500}, {`{"kind":"webdav"}`, "", 501},
		{`{"kind":"s3"}`, "", 503}, {`{"path":"relative"}`, "", 400}, {`{}`, "unreserved", 400},
		{`{}`, store.PublicTargetOperationPrefix + "bad.dot", 400}, {strings.Repeat("x", 64001), "", 413},
	} {
		code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", row.body, row.operation)
		if code != row.code {
			t.Fatal("invalid input admission changed", row.code, code, string(body))
		}
	}
	if peer.calls.Load() != 0 {
		t.Fatal("invalid input reached filesystem peer")
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
		t.Fatal("invalid input changed state", count, err)
	}
}
