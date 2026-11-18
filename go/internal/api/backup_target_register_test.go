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
	entered chan struct{}
	resume  chan struct{}
	control *store.Control
}

func (p *publicTargetPeer) Inspect(ctx context.Context, r *actionv1.InspectFilesystemBackupTargetRequest) (*actionv1.FilesystemBackupTargetInspectionReceipt, error) {
	call := p.calls.Add(1)
	if p.mode == "stalled-first" && call == 1 {
		close(p.entered)
		select {
		case <-p.resume:
		case <-ctx.Done():
			return nil, status.FromContextError(ctx.Err()).Err()
		}
	}
	md, _ := metadata.FromIncomingContext(ctx)
	if values := md.Get("authorization"); len(values) != 1 || values[0] != "Bearer "+strings.Repeat("t", 32) {
		return nil, status.Error(codes.Unauthenticated, "isolated controller credential required")
	}
	if !r.RequireFullContainment {
		return nil, status.Error(codes.InvalidArgument, "complete containment was not requested")
	}
	switch p.mode {
	case "denied":
		return nil, status.Error(codes.InvalidArgument, "Unsafe backup target: isolated containment refusal")
	case "worker-fenced":
		return nil, status.Error(codes.FailedPrecondition, "isolated writer fence mismatch")
	case "worker-unavailable":
		return nil, status.Error(codes.Unavailable, "isolated worker unavailable")
	}
	marker := []byte(`{"targetId":"target_public","targetNonce":"retained","createdAt":"2000-01-01T00:00:00Z"}`)
	if (p.mode == "two-targets" || p.mode == "location-unavailable") && filepath.Base(r.Path) == "other" {
		marker = []byte(`{"targetId":"target_other","targetNonce":"retained","createdAt":"2000-01-01T00:00:00Z"}`)
	}
	if p.mode == "invalid-marker" {
		marker = []byte(`{"targetId":"not-a-target","targetNonce":"retained"}`)
	}
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
	if p.mode == "location-unavailable" {
		return nil, status.Error(codes.Unavailable, "isolated path observation unavailable")
	}
	if p.mode == "store-closed-after-path-proof" && r.ResolvePath {
		if err := p.control.Close(); err != nil {
			return nil, status.Error(codes.Internal, "could not interrupt isolated store")
		}
	}
	o := &actionv1.FilesystemBackupTargetLocationObservation{SchemaVersion: 1, Fence: r.Fence, OperationId: r.OperationId, WriterFencingToken: r.WriterFencingToken,
		RequestedPath: r.Path, ObservedAtUnix: time.Now().Unix(), FleetId: p.binding.FleetID, Environment: p.binding.Environment,
		State: actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING}
	if p.mode == "live-original" {
		o.State = actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS
	}
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
	peer := &publicTargetPeer{mode: mode, binding: control.BackupTargetObservationBinding(), control: control}
	if mode == "stalled-first" {
		peer.entered, peer.resume = make(chan struct{}), make(chan struct{})
	}
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

func TestTargetRegistrationPublicTCPRefusesWorkerAndMetadataFailuresBeforePublication(t *testing.T) {
	for _, failure := range []struct {
		mode, code string
		status     int
	}{
		{"worker-fenced", "BACKUP_TARGET_INSPECTION_DENIED", 503},
		{"worker-unavailable", "BACKUP_TARGET_INSPECTION_UNAVAILABLE", 503},
		{"no-worker-tls", "BACKUP_TARGET_INSPECTION_UNAVAILABLE", 503},
		{"invalid-marker", "invalid_payload", 400},
		{"invalid-metadata", "invalid_payload", 400},
	} {
		t.Run(failure.mode, func(t *testing.T) {
			control, _, server, _ := publicTargetTLSFixture(t, failure.mode)
			if failure.mode == "no-worker-tls" {
				t.Setenv(worker.EnvWorkerTLSCAFile, "")
			}
			payload := map[string]any{"path": filepath.Join(t.TempDir(), "target")}
			if failure.mode == "invalid-metadata" {
				payload["maxReadBytesPerSecond"] = false
			}
			raw, _ := json.Marshal(payload)
			code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(raw), "")
			if code != failure.status || !strings.Contains(string(body), failure.code) {
				t.Fatalf("refusal changed: %d %s", code, body)
			}
			if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
				t.Fatal("unverified request left an operator receipt", count, err)
			}
		})
	}
}

func TestTargetRegistrationPublicTCPDoesNotInferUnavailableLocations(t *testing.T) {
	control, peer, server, _ := publicTargetTLSFixture(t, "location-unavailable")
	root := t.TempDir()
	for index, path := range []string{filepath.Join(root, "first"), filepath.Join(root, "other"), filepath.Join(root, "moved")} {
		raw, _ := json.Marshal(map[string]string{"path": path})
		code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(raw), "")
		if index == 0 && code != http.StatusOK || index > 0 && (code != http.StatusServiceUnavailable ||
			!strings.Contains(string(body), "BACKUP_TARGET_INSPECTION_UNAVAILABLE")) {
			t.Fatalf("location read refusal changed: %d %s", code, body)
		}
	}
	records, _, err := control.ListAuthoritativeTargets()
	if err != nil || len(records) != 1 || records[0].ID != "target_public" || records[0].Revision != 1 || peer.calls.Load() != 5 {
		t.Fatalf("missing read evidence changed the registered registry: %d records, %d calls, %v", len(records), peer.calls.Load(), err)
	}
}

func TestTargetRegistrationPublicTCPRequiresOldLocationProofWhenMoving(t *testing.T) {
	for _, oldExists := range []bool{true, false} {
		name := "removed-original"
		if oldExists {
			name = "live-original"
		}
		t.Run(name, func(t *testing.T) {
			control, peer, server, _ := publicTargetTLSFixture(t, name)
			dir := t.TempDir()
			oldPath, newPath := filepath.Join(dir, "before"), filepath.Join(dir, "after")
			before, _ := json.Marshal(map[string]any{"path": oldPath, "label": "first"})
			after, _ := json.Marshal(map[string]any{"path": newPath, "label": "moved"})
			code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(before), "")
			if code != http.StatusOK {
				t.Fatalf("initial registration failed: %d %s", code, body)
			}
			code, body, _ = publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(after), "")
			wantPath, wantRevision := newPath, int64(2)
			if oldExists {
				wantPath, wantRevision = oldPath, 1
				if code != http.StatusConflict || !strings.Contains(string(body), "target-clone-detected") {
					t.Fatalf("an active clone was not refused: %d %s", code, body)
				}
			} else if code != http.StatusOK {
				t.Fatalf("a signed missing location did not allow the move: %d %s", code, body)
			}
			current, found, err := control.Get("target", "target_public")
			var doc map[string]any
			if err != nil || !found || current.Revision != wantRevision ||
				json.Unmarshal(current.Payload, &doc) != nil || doc["path"] != wantPath || peer.calls.Load() != 3 {
				t.Fatalf("wrong relocation result or proof count: %+v %v %v (%d calls)", current, found, err, peer.calls.Load())
			}
		})
	}
}

func TestTargetRegistrationPublicTCPResolvesOtherTargetBeforePublishing(t *testing.T) {
	control, peer, server, _ := publicTargetTLSFixture(t, "two-targets")
	root := t.TempDir()
	for i, check := range []struct {
		path, id string
		status   int
	}{
		{filepath.Join(root, "first"), "target_public", http.StatusOK},
		{filepath.Join(root, "first", "other"), "", http.StatusBadRequest},
		{filepath.Join(root, "sibling", "other"), "target_other", http.StatusOK},
	} {
		raw, _ := json.Marshal(map[string]any{"path": check.path})
		code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(raw), "")
		if code != check.status || check.id != "" && !strings.Contains(string(body), check.id) ||
			check.id == "" && !strings.Contains(string(body), "overlaps a registered target") {
			t.Fatalf("registration case %d returned %d %s", i, code, body)
		}
	}
	records, _, err := control.ListAuthoritativeTargets()
	if err != nil || len(records) != 2 || peer.calls.Load() != 5 {
		t.Fatalf("unexpected registry or signed observation count: %d records, %d calls, %v", len(records), peer.calls.Load(), err)
	}
}

func TestTargetRegistrationPublicTCPRecoversWhenSnapshotChangesDuringFirstInspection(t *testing.T) {
	for _, test := range []struct {
		name, winnerOperation, winnerLabel, code string
		status                                   int
	}{
		{"same request", "stale-snapshot", "", "", http.StatusOK},
		{"different operation", "separate-operation", "", "revision_conflict", http.StatusConflict},
		{"changed body", "stale-snapshot", "changed", "mutation_request_replay_conflict", http.StatusConflict},
	} {
		t.Run(test.name, func(t *testing.T) {
			assertTargetRegistrationSnapshotRace(t, test.winnerOperation, test.winnerLabel, test.status, test.code)
		})
	}
}

func assertTargetRegistrationSnapshotRace(t *testing.T, winnerOperation, winnerLabel string, wantStatus int, wantCode string) {
	t.Helper()
	control, peer, server, _ := publicTargetTLSFixture(t, "stalled-first")
	path := filepath.Join(t.TempDir(), "target")
	raw, _ := json.Marshal(map[string]any{"path": path, "label": ""})
	winnerBody, _ := json.Marshal(map[string]any{"path": path, "label": winnerLabel})
	operation := store.PublicTargetOperationPrefix + "stale-snapshot"
	request, err := http.NewRequest(http.MethodPost, server.URL+"/api/workspace/backup-targets", strings.NewReader(string(raw)))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer public-target-test-token")
	request.Header.Set("X-DeepSeek-Operation-Id", operation)
	var release sync.Once
	defer release.Do(func() { close(peer.resume) })
	type result struct {
		status int
		body   string
		err    error
	}
	first := make(chan result, 1)
	go func() {
		reply, err := server.Client().Do(request)
		if err != nil {
			first <- result{err: err}
			return
		}
		defer reply.Body.Close()
		body, err := io.ReadAll(reply.Body)
		first <- result{status: reply.StatusCode, body: string(body), err: err}
	}()
	select {
	case <-peer.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the original request did not reach its inspection")
	}
	code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(winnerBody), store.PublicTargetOperationPrefix+winnerOperation)
	if code != http.StatusOK {
		t.Fatalf("concurrent identical request did not commit: %d %s", code, body)
	}
	release.Do(func() { close(peer.resume) })
	replay := <-first
	if replay.err != nil || replay.status != wantStatus ||
		wantStatus == http.StatusOK && replay.body != string(body) ||
		wantStatus != http.StatusOK && !strings.Contains(replay.body, wantCode) {
		t.Fatalf("stale snapshot returned the wrong reply: %+v expected %d %s", replay, wantStatus, wantCode)
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 1 || peer.calls.Load() != 2 {
		t.Fatalf("recovery created duplicate work: %d mutations, %d calls, %v", count, peer.calls.Load(), err)
	}
}

type interruptedTargetBody struct{}

func (interruptedTargetBody) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }

func TestTargetRegistrationPublicBodyAndClosedStoreCannotStartInspection(t *testing.T) {
	control, peer, _, _ := publicTargetTLSFixture(t, "normal")
	view := PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo}
	call := func(body io.Reader) *httptest.ResponseRecorder {
		request := httptest.NewRequest(http.MethodPost, "/api/workspace/backup-targets", body)
		request.Host = "127.0.0.1"
		request.Header.Set("Authorization", "Bearer public-target-test-token")
		recorder := httptest.NewRecorder()
		backupTargetRegister(recorder, request, control, view)
		return recorder
	}
	truncated := call(interruptedTargetBody{})
	if truncated.Code != http.StatusBadRequest || !strings.Contains(truncated.Body.String(), "Request body is unreadable") {
		t.Fatal("incomplete request body was processed", truncated.Code, truncated.Body.String())
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	closed := call(strings.NewReader(`{}`))
	if closed.Code != http.StatusServiceUnavailable || !strings.Contains(closed.Body.String(), "GO_CONTROL_UNAVAILABLE") || peer.calls.Load() != 0 {
		t.Fatal("closed store could send an inspection", closed.Code, closed.Body.String(), peer.calls.Load())
	}
}

func TestTargetRegistrationPublicTCPRejectsRelocationOfAnUnlocatableLegacyIdentity(t *testing.T) {
	control, peer, server, _ := publicTargetTLSFixture(t, "normal")
	legacy := store.Record{Domain: "target", ID: "target_public", State: "ACTIVE", Revision: 1,
		Payload: json.RawMessage(`{"targetId":"target_public","kind":"s3","topologyGeneration":1}`)}
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: "legacy-s3-registration", ActionID: "legacy-s3-action", Actor: "operator:test", Record: legacy,
	}); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(map[string]string{"path": filepath.Join(t.TempDir(), "candidate")})
	code, body, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(raw), "")
	if code != http.StatusConflict || !strings.Contains(string(body), "target-clone-detected") || peer.calls.Load() != 1 {
		t.Fatalf("filesystem registration rebound a non-filesystem identity: %d %s (%d calls)", code, body, peer.calls.Load())
	}
	record, found, err := control.Get("target", legacy.ID)
	if err != nil || !found || record.Revision != 1 || !strings.Contains(string(record.Payload), `"kind":"s3"`) {
		t.Fatal("refused registration lost the existing provider identity", err)
	}
}

func TestTargetRegistrationPublicTCPRequiresCurrentReadAfterCompetingPathProof(t *testing.T) {
	control, peer, server, _ := publicTargetTLSFixture(t, "store-closed-after-path-proof")
	root := t.TempDir()
	registered, _ := json.Marshal(map[string]any{"targetId": "target_other", "kind": "filesystem",
		"path": filepath.Join(root, "retained"), "topologyGeneration": 1})
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: "prior-competing-target", ActionID: "prior-competing-action", Actor: "operator:test",
		Record: store.Record{Domain: "target", ID: "target_other", State: "ACTIVE", Revision: 1, Payload: registered},
	}); err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(map[string]string{"path": filepath.Join(root, "new-target")})
	statusCode, response, _ := publicTargetHTTPRequest(t, server, http.MethodPost, "/api/workspace/backup-targets", string(body), "")
	if statusCode != http.StatusServiceUnavailable || !strings.Contains(string(response), "GO_CONTROL_UNAVAILABLE") || peer.calls.Load() != 2 {
		t.Fatal("Go published a response after the current identity read became unavailable", statusCode, string(response), peer.calls.Load())
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
