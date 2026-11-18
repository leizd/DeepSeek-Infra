//go:build integration

package worker

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// The actual Rust executable provisions and retains its own custody key. Go
// reads only public manifest metadata and a public transport CA certificate.
// Temporary marker bytes are a read fixture, not production initialization.
func TestRustTargetObservationGoClientAgainstRealCustodyAndFilesystem(t *testing.T) {
	binary := os.Getenv("DEEPSEEK_TEST_RUST_WORKER_BINARY")
	external := os.Getenv("DEEPSEEK_TEST_TARGET_EXTERNAL_ROOT")
	if !filepath.IsAbs(binary) || !filepath.IsAbs(external) {
		t.Fatal("explicit cloud Rust executable and external test root required")
	}
	base, err := os.MkdirTemp(external, "native-target-observation-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(base); err != nil {
			t.Error(err)
		}
	})
	base, err = filepath.EvalSymlinks(base)
	if err != nil {
		t.Fatal(err)
	}
	managed, runtime := filepath.Join(base, "managed"), filepath.Join(base, "runtime")
	for _, directory := range []string{managed, runtime} {
		if err := os.Mkdir(directory, 0700); err != nil {
			t.Fatal(err)
		}
	}
	processCtx, cancel := context.WithTimeout(context.Background(), 150*time.Second)
	t.Cleanup(cancel)
	const profile = "0123456789abcdef0123456789abcdef"
	env := []string{}
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(strings.ToUpper(entry), "DEEPSEEK_") {
			env = append(env, entry)
		}
	}
	env = append(env, "DEEPSEEK_WORKER_MANAGED_ROOT="+managed, "DEEPSEEK_WORKER_MANAGED_PROFILE_ID="+profile)
	initialize := exec.CommandContext(processCtx, binary)
	hideTestWorker(initialize)
	initialize.Env = append(append([]string(nil), env...), "DEEPSEEK_WORKER_MANAGED_INITIALIZE=1")
	if err := initialize.Run(); err != nil {
		t.Fatal("isolated Rust custody initialization failed", err)
	}
	public, err := os.ReadFile(filepath.Join(managed, "custody", "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		PublicKey   string `json:"signer_public_key"`
		FleetID     string `json:"fleet_id"`
		Environment string `json:"environment"`
	}
	if err := json.Unmarshal(public, &manifest); err != nil {
		t.Fatal(err)
	}
	binding := ControlSignerBinding{PublicKey: manifest.PublicKey, FleetID: manifest.FleetID, Environment: manifest.Environment}
	if !validObservationBinding(binding) || binding.FleetID != "native-"+profile || binding.Environment != "local" {
		t.Fatal("Rust public deployment metadata missing")
	}
	// The normal python_disabled startup requires its private control transport
	// before opening its journals. Read observations must never call this peer.
	// A later phase admits just one explicitly expected marker preparation.
	// Rust reads its own private client key file.
	controlMaterial := generateTLSMaterial(t, t.TempDir(), "deepseek-control.test")
	controlCertificate, err := tls.LoadX509KeyPair(controlMaterial.certFile, controlMaterial.keyFile)
	if err != nil {
		t.Fatal(err)
	}
	rustCA, err := os.ReadFile(filepath.Join(managed, "custody", "transport-ca.pem"))
	if err != nil {
		t.Fatal(err)
	}
	clientRoots := x509.NewCertPool()
	if !clientRoots.AppendCertsFromPEM(rustCA) {
		t.Fatal("Rust public transport CA invalid")
	}
	controlListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var controlCalls atomic.Int64
	var expectedClaim atomic.Pointer[actionv1.TargetMarkerClaim]
	var dispatched atomic.Bool
	controlServer := grpc.NewServer(grpc.Creds(credentials.NewTLS(&tls.Config{
		Certificates: []tls.Certificate{controlCertificate}, ClientAuth: tls.RequireAndVerifyClientCert,
		ClientCAs: clientRoots, MinVersion: tls.VersionTLS12,
	})), grpc.UnaryInterceptor(func(ctx context.Context, request any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		controlCalls.Add(1)
		marker, ok := request.(*actionv1.TargetMarkerDispatchRequest)
		headers, _ := metadata.FromIncomingContext(ctx)
		auth := headers.Get("authorization")
		if expected := expectedClaim.Load(); ok && expected != nil && len(auth) == 1 && auth[0] == "Bearer "+tlsTestSecret &&
			proto.Equal(expected, marker.Claim) && proto.Equal(marker.Fence, expected.Fence) && len(marker.PreparationSha256) == 32 &&
			dispatched.CompareAndSwap(false, true) {
			return &actionv1.TargetMarkerDispatchPermit{SchemaVersion: 1, PreparationSha256: marker.PreparationSha256,
				LeaseUntilUnix: expected.LeaseUntilUnix}, nil
		}
		return nil, status.Error(codes.PermissionDenied, "read-only fixture never grants control commands")
	}))
	controlv1.RegisterControlPlaneServer(controlServer, &controlv1.UnimplementedControlPlaneServer{})
	actionv1.RegisterBackupTargetMarkerControlServer(controlServer, &actionv1.UnimplementedBackupTargetMarkerControlServer{})
	go func() { _ = controlServer.Serve(controlListener) }()
	t.Cleanup(controlServer.Stop)
	env = append(env,
		"DEEPSEEK_GO_CONTROL_RPC_URL=https://"+controlListener.Addr().String(),
		"DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE="+controlMaterial.caFile,
		"DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE="+filepath.Join(managed, "custody", "control-client.pem"),
		"DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE="+filepath.Join(managed, "custody", "control-client-key.pem"),
		"DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME=deepseek-control.test", "DEEPSEEK_INTERNAL_BEARER="+tlsTestSecret)
	expires := time.Now().UTC().Add(10 * time.Minute).Truncate(time.Second)
	workerEnv := append(append([]string(nil), env...),
		"DEEPSEEK_INFRA_ROOT="+runtime, "DEEPSEEK_RUNTIME_MODE=python_disabled",
		EnvWorkerServiceBearer+"="+tlsTestSecret, EnvWorkerServiceBearerExpires+"="+expires.Format("2006-01-02T15:04:05Z"),
		EnvWorkerServiceName+"=go-control-plane", EnvWorkerServiceRole+"=controller")
	client, stopWorker := startTargetObservationWorker(t, processCtx, binary, workerEnv, managed, "7", expires)
	// Test the established read protocol before admitting the marker mutation.
	directory := filepath.Join(base, "retained-target")
	empty := filepath.Join(base, "empty-target")
	for _, path := range []string{directory, empty} {
		if err := os.Mkdir(path, 0700); err != nil {
			t.Fatal(err)
		}
	}
	verifyTargetReads(t, processCtx, client, binding, base, directory, empty)
	if controlCalls.Load() != 0 {
		t.Fatal("read observations attempted a control command", controlCalls.Load())
	}
	t.Log("actual Rust custody and TLS observation verified; original bytes retained; no marker initialization or effect epoch admission")

	operation, marker := initializePhysicalTargetWithLostReply(t, processCtx, client, binding, empty, &expectedClaim)
	if !dispatched.Load() || controlCalls.Load() != 1 {
		t.Fatal("Rust did not obtain exactly one authenticated dispatch permit", controlCalls.Load())
	}
	stopWorker()
	restarted, _ := startTargetObservationWorker(t, processCtx, binary, workerEnv, managed, "8", expires)
	operation.State = "DISPATCHED"
	receipt, err := restarted.ResolveTargetMarker(processCtx, operation, 8)
	if err != nil || receipt.Effect == nil || receipt.SignerPublicKey != binding.PublicKey ||
		!proto.Equal(receipt.Effect.Fence, operation.Claim.Fence) || !bytes.Equal(receipt.Effect.MarkerJson, marker) ||
		!bytes.Equal(receipt.Effect.ClaimSha256, operation.ClaimSHA256) || !bytes.Equal(receipt.Effect.DirectoryIdentity, operation.Claim.DirectoryIdentity) {
		t.Fatal("Rust restart did not recover original marker effect", err)
	}
	key, err := base64.RawURLEncoding.DecodeString(receipt.SignerPublicKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		t.Fatal("pinned Rust signer key malformed")
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Effect)
	if err != nil || !ed25519.Verify(key, append([]byte("deepseek-infra:backup-target-marker-effect-v1\x00"), wire...), receipt.Signature) {
		t.Fatal("Go could not verify original Rust effect signature", err)
	}
	after, err := os.ReadFile(filepath.Join(empty, ".deepseek-infra-backup-target.json"))
	if err != nil || !bytes.Equal(marker, after) {
		t.Fatal("reconciliation changed the physical marker", err)
	}
	operation.State = "CLAIMED"
	if _, err := restarted.ResolveTargetMarker(processCtx, operation, 8); status.Code(err) != codes.FailedPrecondition || controlCalls.Load() != 1 {
		t.Fatal("successor sent another dispatch for an old writer", err, controlCalls.Load())
	}
	t.Log("isolated mTLS fixture: real Rust marker persisted before lost reply; new writer recovered the original signed effect")
}

func startTargetObservationWorker(t *testing.T, processCtx context.Context, binary string, env []string, managed, writer string, expires time.Time) (*Client, func()) {
	t.Helper()
	reservation, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	target := reservation.Addr().String()
	if err := reservation.Close(); err != nil {
		t.Fatal(err)
	}
	command := exec.CommandContext(processCtx, binary)
	hideTestWorker(command)
	command.Env = append(append([]string(nil), env...),
		"DEEPSEEK_WORKER_MANAGED_FENCING_TOKEN="+writer, "DEEPSEEK_WORKER_LISTEN="+target)
	var output bytes.Buffer
	command.Stdout, command.Stderr = &output, &output
	command.WaitDelay = 2 * time.Second
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan struct{})
	go func() { _ = command.Wait(); close(exited) }()
	stopWorker := sync.OnceFunc(func() {
		_ = command.Process.Kill()
		select {
		case <-exited:
			logs := output.String()
			if strings.Contains(logs, tlsTestSecret) || strings.Contains(logs, "PRIVATE KEY") {
				t.Error("worker exposed credential bytes; diagnostic output withheld")
			} else if t.Failed() {
				if len(logs) > 8192 {
					logs = logs[len(logs)-8192:]
				}
				t.Logf("isolated Rust process diagnostic after confirmed exit: %s", logs)
			}
		case <-time.After(5 * time.Second):
			t.Error("isolated Rust process was not reaped")
		}
	})
	t.Cleanup(stopWorker)
	ready, stopReady := context.WithTimeout(processCtx, 20*time.Second)
	defer stopReady()
	for {
		connection, err := net.DialTimeout("tcp", target, 100*time.Millisecond)
		if err == nil {
			_ = connection.Close()
			break
		}
		select {
		case <-exited:
			t.Fatal("isolated Rust worker exited before readiness")
		case <-ready.Done():
			t.Fatal("isolated Rust worker did not become ready")
		case <-time.After(50 * time.Millisecond):
		}
	}
	client, err := DialTLS(TLSDialConfig{Target: target, TrustRootFile: filepath.Join(managed, "custody", "transport-ca.pem"),
		ServerName: "deepseek-worker.local", BearerToken: tlsTestSecret, ExpiresAt: expires})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client, stopWorker
}

func verifyTargetReads(t *testing.T, processCtx context.Context, client *Client, binding ControlSignerBinding, base, directory, empty string) {
	t.Helper()
	markerPath := filepath.Join(directory, ".deepseek-infra-backup-target.json")
	marker := []byte("{\n  \"targetId\":\"target_original\",\"targetNonce\":\"retained-fixture\",\"targetGeneration\":9007199254740993\n}\n")
	if err := os.WriteFile(markerPath, marker, 0600); err != nil {
		t.Fatal(err)
	}
	request := &actionv1.InspectFilesystemBackupTargetRequest{SchemaVersion: 1, OperationId: "target-inspect-real-go-rust",
		Fence: &commonv1.ActionFence{ActionId: "target-inspect-real-go-rust", ExecutionEpoch: 3}, WriterFencingToken: 7, Path: directory}
	ctx, stop := context.WithTimeout(processCtx, 30*time.Second)
	defer stop()
	if err := client.Admit(ctx, actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, request.Fence); !errors.Is(err, internalprotocol.ErrFenceMismatch) {
		t.Fatal("unexpected pre-read effect admission", err)
	}
	inspection, err := client.InspectFilesystemBackupTarget(ctx, request, binding)
	if err != nil || inspection == nil || !inspection.Observation.MarkerPresent || !bytes.Equal(inspection.Observation.MarkerJson, marker) || inspection.Observation.CanonicalPath != directory {
		t.Fatal("real Go-to-Rust marker inspection failed", err)
	}
	completeRequest := proto.Clone(request).(*actionv1.InspectFilesystemBackupTargetRequest)
	completeRequest.RequireFullContainment = true
	complete, err := client.InspectFilesystemBackupTarget(ctx, completeRequest, binding)
	if err != nil || complete == nil || !complete.Observation.FullContainmentChecked || !bytes.Equal(complete.Observation.MarkerJson, marker) {
		t.Fatal("real Rust did not sign complete containment with original marker bytes", err)
	}
	t.Log("actual Rust full-containment request and signed receipt verified without marker mutation")
	absentRequest := proto.Clone(request).(*actionv1.InspectFilesystemBackupTargetRequest)
	absentRequest.Path = empty
	absent, err := client.InspectFilesystemBackupTarget(ctx, absentRequest, binding)
	if err != nil || absent == nil || absent.Observation.MarkerPresent || len(absent.Observation.MarkerJson) != 0 {
		t.Fatal("real signed marker absence failed", err)
	}
	for _, sample := range []struct {
		path  string
		state actionv1.BackupTargetLocationState
	}{
		{directory, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS},
		{markerPath, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS},
		{filepath.Join(base, "missing-target"), actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING},
	} {
		location := targetLocationRequest(request)
		location.Path = sample.path
		result, err := client.ProbeFilesystemBackupTargetLocation(ctx, location, binding)
		if err != nil || result == nil || result.Observation.State != sample.state {
			t.Fatal("real signed existence observation failed", err)
		}
	}
	// Offline retained Python is the comparison oracle only. The actual worker
	// resolves and signs these reads in Rust over the production TLS client.
	if err := os.MkdirAll(filepath.Join(base, "resolved-real", "nested"), 0700); err != nil {
		t.Fatal(err)
	}
	for name, target := range map[string]string{"resolved-link": "resolved-real/nested", "resolved-broken": "uncreated/child", "resolved-loop": "resolved-loop"} {
		if err := os.Symlink(target, filepath.Join(base, name)); err != nil {
			t.Fatal(err)
		}
	}
	paths := []string{directory, base + "/missing-target/child", markerPath + "/child", base + "/resolved-link/../leaf",
		base + "/resolved-link/missing/../leaf", base + "/resolved-broken/../leaf", base + "/resolved-loop", base + "/resolved-loop/.."}
	encodedPaths, err := json.Marshal(paths)
	if err != nil {
		t.Fatal(err)
	}
	oracleScript := `import json,pathlib,sys
assert (3,10) <= sys.version_info[:2] <= (3,12), sys.version
out=[]
for raw in json.loads(sys.argv[1]):
    try: out.append({'path':raw,'resolved':str(pathlib.Path(raw).resolve()),'loop':False})
    except RuntimeError: out.append({'path':raw,'resolved':'','loop':True})
print(json.dumps({'pythonVersion':sys.version.split()[0],'cases':out}))`
	oracleBytes, err := exec.CommandContext(ctx, "python", "-I", "-S", "-c", oracleScript, string(encodedPaths)).CombinedOutput()
	if err != nil || len(oracleBytes) > 128*1024 {
		t.Fatal("retained offline path oracle failed", err, string(oracleBytes))
	}
	var oracle struct {
		PythonVersion string
		Cases         []struct {
			Path, Resolved string
			Loop           bool
		}
	}
	if err := json.Unmarshal(oracleBytes, &oracle); err != nil || len(oracle.Cases) != len(paths) {
		t.Fatal("path oracle report invalid", err)
	}
	for index, expected := range oracle.Cases {
		if expected.Path != paths[index] {
			t.Fatal("oracle changed the requested case order")
		}
		location := targetLocationRequest(request)
		location.Path, location.ResolvePath = expected.Path, true
		result, err := client.ProbeFilesystemBackupTargetLocation(ctx, location, binding)
		if expected.Loop {
			if status.Code(err) != codes.Unavailable {
				t.Fatal("Rust failed to retain the path loop error", err)
			}
		} else if err != nil || result == nil || result.Observation.CanonicalPath != expected.Resolved {
			t.Fatalf("Rust/retained-Python path mismatch: case=%d expected=%q result=%+v error=%v", index, expected.Resolved, result, err)
		}
	}
	t.Logf("actual Go/TLS/Rust custody path resolution matched %d retained CPython %s cases", len(paths), oracle.PythonVersion)
	wrongWriter := proto.Clone(request).(*actionv1.InspectFilesystemBackupTargetRequest)
	wrongWriter.WriterFencingToken++
	if _, err := client.InspectFilesystemBackupTarget(ctx, wrongWriter, binding); status.Code(err) != codes.FailedPrecondition {
		t.Fatal("Rust accepted the wrong writer", err)
	}
	if _, err := client.ProbeFilesystemBackupTargetLocation(ctx, targetLocationRequest(wrongWriter), binding); status.Code(err) != codes.FailedPrecondition {
		t.Fatal("Rust location accepted the wrong writer", err)
	}
	wrongBinding := binding
	wrongBinding.FleetID += "-other"
	if _, err := client.InspectFilesystemBackupTarget(ctx, request, wrongBinding); !errors.Is(err, ErrTargetInspectionResponse) {
		t.Fatal("Go accepted another deployment", err)
	}
	if _, err := client.ProbeFilesystemBackupTargetLocation(ctx, targetLocationRequest(request), wrongBinding); !errors.Is(err, ErrTargetInspectionResponse) {
		t.Fatal("Go accepted another location deployment", err)
	}
	after, err := os.ReadFile(markerPath)
	if err != nil || !bytes.Equal(marker, after) {
		t.Fatal("observation changed original marker bytes", err)
	}
	if _, err := os.Stat(filepath.Join(empty, ".deepseek-infra-backup-target.json")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("read initialized an empty target", err)
	}
	if _, err := os.Stat(filepath.Join(base, "missing-target")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("location observation created a missing path", err)
	}
	if err := client.Admit(ctx, actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, request.Fence); !errors.Is(err, internalprotocol.ErrFenceMismatch) {
		t.Fatal("read installed an effect epoch", err)
	}
}

type discardMarkerReply struct {
	actionv1.BackupTargetMarkerWorkerClient
}

func (client discardMarkerReply) Initialize(ctx context.Context, request *actionv1.TargetMarkerClaim, options ...grpc.CallOption) (*actionv1.TargetMarkerReceipt, error) {
	if _, err := client.BackupTargetMarkerWorkerClient.Initialize(ctx, request, options...); err != nil {
		return nil, err
	}
	return nil, status.Error(codes.Unavailable, "fixture discarded successful Rust marker reply")
}

func initializePhysicalTargetWithLostReply(t *testing.T, ctx context.Context, client *Client, binding ControlSignerBinding,
	directory string, expected *atomic.Pointer[actionv1.TargetMarkerClaim]) (store.TargetMarkerOperation, []byte) {
	t.Helper()
	const operation = "target-inspect-public-v1-physical-reply-loss"
	fence := &commonv1.ActionFence{ActionId: operation, ExecutionEpoch: 3}
	observation, err := client.InspectFilesystemBackupTarget(ctx, &actionv1.InspectFilesystemBackupTargetRequest{
		SchemaVersion: 1, OperationId: operation, Fence: fence, WriterFencingToken: 7, Path: directory, RequireFullContainment: true,
	}, binding)
	if err != nil || observation.Observation.MarkerPresent || len(observation.Observation.DirectoryIdentity) != 32 {
		t.Fatal("legal empty directory did not get a signed pinned identity", err)
	}
	now := time.Now().Unix()
	requestHash := sha256.Sum256([]byte(directory))
	claim := &actionv1.TargetMarkerClaim{
		SchemaVersion: 1, Fence: fence, OperationId: operation, WriterFencingToken: 7, CutoverRevision: 4,
		IssuedAtUnix: now, LeaseUntilUnix: now + 30, RequestedPath: directory, CanonicalPath: directory,
		TargetId: "target_030303030303", RequestSha256: requestHash[:], Nonce: bytes.Repeat([]byte{3}, 32),
		FleetId: binding.FleetID, Environment: binding.Environment, DirectoryIdentity: observation.Observation.DirectoryIdentity,
	}
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(claim)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(encoded)
	active := store.TargetMarkerOperation{Claim: claim, ClaimSHA256: digest[:], State: "CLAIMED"}
	expected.Store(claim)
	faulted := *client
	faulted.targetMarker = discardMarkerReply{client.targetMarker}
	if _, err := faulted.ResolveTargetMarker(ctx, active, 7); status.Convert(err).Message() != "fixture discarded successful Rust marker reply" {
		t.Fatal("call did not complete real marker publication before the reply fault", err)
	}
	marker, err := os.ReadFile(filepath.Join(directory, ".deepseek-infra-backup-target.json"))
	if err != nil || !json.Valid(marker) {
		t.Fatal("marker was not durably present after response loss", err)
	}
	return active, marker
}
