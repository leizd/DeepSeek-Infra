//go:build integration

package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc/codes"
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
	reservation, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	target := reservation.Addr().String()
	if err := reservation.Close(); err != nil {
		t.Fatal(err)
	}
	expires := time.Now().UTC().Add(10 * time.Minute).Truncate(time.Second)
	command := exec.CommandContext(processCtx, binary)
	hideTestWorker(command)
	command.Env = append(append([]string(nil), env...),
		"DEEPSEEK_WORKER_MANAGED_FENCING_TOKEN=7", "DEEPSEEK_WORKER_LISTEN="+target,
		"DEEPSEEK_INFRA_ROOT="+runtime, "DEEPSEEK_RUNTIME_MODE=python_disabled",
		EnvWorkerServiceBearer+"="+tlsTestSecret, EnvWorkerServiceBearerExpires+"="+expires.Format("2006-01-02T15:04:05Z"),
		EnvWorkerServiceName+"=go-control-plane", EnvWorkerServiceRole+"=controller")
	var output bytes.Buffer
	command.Stdout, command.Stderr = &output, &output
	command.WaitDelay = 2 * time.Second
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan struct{})
	go func() { _ = command.Wait(); close(exited) }()
	t.Cleanup(func() {
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
	directory := filepath.Join(base, "retained-target")
	empty := filepath.Join(base, "empty-target")
	for _, path := range []string{directory, empty} {
		if err := os.Mkdir(path, 0700); err != nil {
			t.Fatal(err)
		}
	}
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
	t.Log("actual Rust custody and TLS observation verified; original bytes retained; no marker initialization or effect epoch admission")
}
