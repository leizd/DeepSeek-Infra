package launch

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func managedFailurePlan(t *testing.T, root string) ([]Process, []string) {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	env := []string{managedRequired + "=1", "DEEPSEEK_RUNTIME_MODE=python_disabled", "DEEPSEEKD_MODE=authoritative", "DEEPSEEK_INFRA_ROOT=" + root, "DEEPSEEKD_PRODUCTION_STORE=" + filepath.Join(root, "go-control"), "DEEPSEEK_WORKER_LISTEN=" + address, "DEEPSEEK_LAUNCH_HELPER=1"}
	processes := []Process{}
	for _, role := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway"} {
		processes = append(processes, Process{Name: role, Path: executable, Env: append(append([]string(nil), env...), "DEEPSEEK_MANAGED_FIXTURE_ROLE="+role)})
	}
	return processes, env
}
func assertManagedFailureRetainsCustodyWithoutControl(t *testing.T, root string) {
	t.Helper()
	for _, name := range []string{"fixture-control-ready", "fixture-gateway-ready", filepath.Join("go-control", store.ControlDatabaseFilename)} {
		if _, err := os.Stat(filepath.Join(root, name)); !os.IsNotExist(err) {
			t.Fatal("failed bootstrap started control or gateway")
		}
	}
	entries, err := os.ReadDir(filepath.Join(root, "go-control/native-profile"))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), "transport-") {
			t.Fatal("failed startup retained a Go transport session")
		}
	}
}
func TestManagedBootstrapFaultsNeverStartControlOrReplaceCustody(t *testing.T) {
	for _, failure := range []string{"initialize", "invalid-ca", "session", "directory-permission", "identity", "write-ca", "write-cert", "write-key", "worker-bearer", "control-bearer", "binding", "wrong-bearer", "wrong-trust-root", "publish", "publish-conflict"} {
		t.Run(failure, func(t *testing.T) {
			root := t.TempDir()
			processes, env := managedFailurePlan(t, root)
			ops := nativeManagedOperations()
			fault := errors.New("offline managed bootstrap fault")
			switch failure {
			case "initialize":
				ops.initialize = func(context.Context, Process, []string, io.Writer) error { return fault }
			case "invalid-ca", "wrong-trust-root":
				original := ops.initialize
				ops.initialize = func(ctx context.Context, p Process, e []string, out io.Writer) error {
					if err := original(ctx, p, e, out); err != nil {
						return err
					}
					bytes := []byte("not a certificate")
					if failure == "wrong-trust-root" {
						var err error
						bytes, _, _, err = managedGoIdentity(time.Now())
						if err != nil {
							return err
						}
					}
					return os.WriteFile(filepath.Join(root, "rust-worker/custody/transport-ca.pem"), bytes, 0o600)
				}
			case "session":
				ops.session = func(string, string) (string, error) { return "", fault }
			case "directory-permission":
				ops.privateDirectory = func(string) error { return fault }
			case "identity":
				ops.identity = func(time.Time) ([]byte, []byte, []byte, error) { return nil, nil, nil, fault }
			case "write-ca", "write-cert", "write-key":
				original := ops.writeFile
				want := map[string]string{"write-ca": "go-ca.pem", "write-cert": "go-server.pem", "write-key": "go-server-key.pem"}[failure]
				ops.writeFile = func(path string, bytes []byte, mode os.FileMode) error {
					if filepath.Base(path) == want {
						return fault
					}
					return original(path, bytes, mode)
				}
			case "worker-bearer", "control-bearer":
				original := ops.random
				calls := 0
				at := 1
				if failure == "control-bearer" {
					at = 2
				}
				ops.random = func(n int) (string, error) {
					calls++
					if calls == at {
						return "", fault
					}
					return original(n)
				}
			case "binding":
				ops.binding = func(context.Context, worker.TLSDialConfig, worker.ControlSignerBindingQuery) (worker.ControlSignerBinding, error) {
					return worker.ControlSignerBinding{}, fault
				}
			case "wrong-bearer":
				original := ops.binding
				ops.binding = func(ctx context.Context, cfg worker.TLSDialConfig, q worker.ControlSignerBindingQuery) (worker.ControlSignerBinding, error) {
					cfg.BearerToken = strings.Repeat("Z", 64)
					return original(ctx, cfg, q)
				}
			case "publish":
				ops.publish = func(string, []byte) error { return fault }
			case "publish-conflict":
				original := ops.publish
				ops.publish = func(path string, value []byte) error {
					if err := os.Mkdir(path, 0o700); err != nil {
						return err
					}
					return original(path, value)
				}
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			err := runManagedRuntimeWith(ctx, processes, env, io.Discard, ops)
			if err == nil {
				t.Fatal("injected startup fault was ignored")
			}
			switch failure {
			case "invalid-ca":
				if !errors.Is(err, errManagedProfile) {
					t.Fatalf("invalid trust root failed at a different stage: %v", err)
				}
			case "wrong-bearer":
				if status.Code(err) != codes.Unauthenticated {
					t.Fatalf("credential rejection was not observed: %v", err)
				}
			case "wrong-trust-root":
				if err.Error() != "managed Rust signer did not become ready" {
					t.Fatalf("TLS trust refusal was not observed: %v", err)
				}
			case "publish-conflict":
				if !errors.Is(err, errManagedProfile) {
					t.Fatalf("pin publication conflict failed at a different stage: %v", err)
				}
			default:
				if !errors.Is(err, fault) {
					t.Fatalf("injected fault was not reached: %v", err)
				}
			}
			assertManagedFailureRetainsCustodyWithoutControl(t, root)
			if failure != "initialize" {
				if _, err := os.Stat(filepath.Join(root, "rust-worker/custody/worker-key.pem")); err != nil {
					t.Fatal("failure deleted Rust custody")
				}
			}
		})
	}
}
func TestManagedBootstrapRejectsCorruptMetadataBeforeInitialization(t *testing.T) {
	for _, failure := range []string{"metadata-file", "lock-directory", "identity-directory", "identity-hex", "pin-directory", "pin-value", "rust-file"} {
		t.Run(failure, func(t *testing.T) {
			root := t.TempDir()
			profile, err := openManagedProfile(root)
			if err != nil {
				t.Fatal(err)
			}
			meta := profile.metadata
			profile.close()
			switch failure {
			case "metadata-file":
				other := t.TempDir()
				if err = os.Mkdir(filepath.Join(other, "go-control"), 0o700); err != nil {
					t.Fatal(err)
				}
				if err = os.WriteFile(filepath.Join(other, "go-control/native-profile"), []byte("retained metadata"), 0o600); err != nil {
					t.Fatal(err)
				}
				root = other
			case "lock-directory":
				path := filepath.Join(meta, "launch.lock")
				if err = os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err = os.Mkdir(path, 0o700); err != nil {
					t.Fatal(err)
				}
			case "identity-directory":
				path := filepath.Join(meta, "profile-id")
				if err = os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if err = os.Mkdir(path, 0o700); err != nil {
					t.Fatal(err)
				}
			case "identity-hex":
				err = os.WriteFile(filepath.Join(meta, "profile-id"), []byte(strings.Repeat("g", 32)), 0o600)
			case "pin-directory":
				err = os.Mkdir(filepath.Join(meta, "signer-public-pin"), 0o700)
			case "pin-value":
				err = os.WriteFile(filepath.Join(meta, "signer-public-pin"), []byte("not a public signing key"), 0o600)
			case "rust-file":
				path := filepath.Join(root, "rust-worker")
				if err = os.Remove(path); err != nil {
					t.Fatal(err)
				}
				err = os.WriteFile(path, []byte("retained Rust marker"), 0o600)
			}
			if err != nil {
				t.Fatal(err)
			}
			if profile, err = openManagedProfile(root); err == nil {
				profile.close()
				t.Fatal("corrupt metadata reached initialization")
			}
		})
	}
}
func TestManagedFreshnessRequiresReadableExclusiveNativeMetadata(t *testing.T) {
	for _, kind := range []string{"authentication-file", "authentication-directory", "unknown-metadata"} {
		t.Run(kind, func(t *testing.T) {
			root := t.TempDir()
			auth := filepath.Join(root, ".auth-token")
			switch kind {
			case "authentication-file":
				if err := os.WriteFile(auth, []byte("retained-offline-auth-fixture"), 0o600); err != nil {
					t.Fatal(err)
				}
			case "authentication-directory":
				if err := os.Mkdir(auth, 0o700); err != nil {
					t.Fatal(err)
				}
			case "unknown-metadata":
				meta := filepath.Join(root, "go-control/native-profile")
				if err := os.MkdirAll(meta, 0o700); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(meta, "retained-partial-identity"), []byte("do not discard"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			profile, err := openManagedProfile(root)
			if kind == "authentication-file" {
				if err != nil {
					t.Fatal(err)
				}
				profile.close()
				bytes, _ := os.ReadFile(auth)
				if string(bytes) != "retained-offline-auth-fixture" {
					t.Fatal("local authentication replaced")
				}
			} else if err == nil {
				profile.close()
				t.Fatal("uncertain freshness was accepted")
			}
		})
	}
	root := t.TempDir()
	if initiallyEmptyNativeProfile(filepath.Join(root, "missing"), filepath.Join(root, "missing-meta")) {
		t.Fatal("missing source counted as empty")
	}
	if err := os.WriteFile(filepath.Join(root, "meta-file"), []byte("retained"), 0o600); err != nil {
		t.Fatal(err)
	}
	empty := t.TempDir()
	if initiallyEmptyNativeProfile(empty, filepath.Join(root, "meta-file")) {
		t.Fatal("unreadable metadata counted as empty")
	}
}

type managedReadFailure struct{}

func (managedReadFailure) Read([]byte) (int, error) { return 0, errors.New("offline I/O fault") }
func TestManagedBoundedReadsRefuseTruncationAndIOFailure(t *testing.T) {
	if err := managedDirectory(string([]byte{0})); err == nil {
		t.Fatal("invalid directory path accepted")
	}
	file := filepath.Join(t.TempDir(), "retained-file")
	if err := os.WriteFile(file, []byte("retain"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := secureManagedDirectory(filepath.Join(file, "child")); err == nil {
		t.Fatal("private directory replaced a file")
	}
	for _, reader := range []io.Reader{managedReadFailure{}, strings.NewReader(""), strings.NewReader("grew beyond the advertised size")} {
		if value, err := readManagedValue(reader, 1); err == nil || value != nil {
			t.Fatal("partial or changed metadata accepted")
		}
	}
	if value, err := readManagedValue(strings.NewReader("retained"), 20); err != nil || string(value) != "retained" {
		t.Fatal("bounded metadata read failed")
	}
	if !managedPathsEqualForOS("C:/Native", "c:/native", "windows") || managedPathsEqualForOS("/Native", "/native", "linux") {
		t.Fatal("platform path equality changed")
	}
	if _, _, _, err := managedGoIdentity(time.Date(10001, 1, 1, 0, 0, 0, 0, time.UTC)); err == nil {
		t.Fatal("unencodable certificate time accepted")
	}
	root := t.TempDir()
	if err := publishManagedValue(filepath.Join(root, "missing/metadata"), []byte("keep")); err == nil {
		t.Fatal("publication without a parent succeeded")
	}
}
func TestManagedInitializerRequiresFenceAndReportsRealChildFailure(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	process := Process{Name: "deepseek-worker", Path: executable}
	fault := errors.New("offline mechanical fence fault")
	factory := func() (supervisorFence, error) { return nil, fault }
	if err = runManagedInitializerWithFence(context.Background(), process, nil, io.Discard, factory); !errors.Is(err, fault) {
		t.Fatal(err)
	}
	for _, phase := range []string{"prepare", "admit"} {
		fence := &rejectingFence{}
		if phase == "prepare" {
			fence.preparationFailure = fault
		} else {
			fence.admission = func(int) error { return fault }
		}
		env := []string{"DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_MANAGED_FIXTURE_ROLE=deepseek-worker", "DEEPSEEK_WORKER_MANAGED_INITIALIZE=1", "DEEPSEEK_WORKER_MANAGED_ROOT=" + filepath.Join(t.TempDir(), "absent-parent")}
		if err = runManagedInitializerWithFence(context.Background(), process, env, io.Discard, func() (supervisorFence, error) { return fence, nil }); !errors.Is(err, fault) || !fence.closed {
			t.Fatal("initializer bypassed mechanical fence")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err = runManagedInitializer(ctx, process, nil, io.Discard); err == nil {
		t.Fatal("cancelled initializer started")
	}
	env := []string{"DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_MANAGED_FIXTURE_ROLE=deepseek-worker", "DEEPSEEK_WORKER_MANAGED_INITIALIZE=1", "DEEPSEEK_WORKER_MANAGED_ROOT=" + filepath.Join(t.TempDir(), "absent-parent")}
	if err = runManagedInitializer(context.Background(), process, env, io.Discard); err == nil {
		t.Fatal("failed custody process accepted")
	}
}
func TestManagedRuntimeRejectsInvalidPlansAndUnavailableBinding(t *testing.T) {
	root := t.TempDir()
	processes, env := managedFailurePlan(t, root)
	for _, bad := range [][]Process{append(append([]Process(nil), processes...), processes[0]), {{Name: "foreign", Path: processes[0].Path}}, {{Name: "deepseekd", Path: "relative"}}} {
		if err := runManagedRuntime(context.Background(), bad, env, io.Discard); err == nil {
			t.Fatal("invalid plan accepted")
		}
	}
	badEnv := append(append([]string(nil), env...), "DEEPSEEK_INFRA_ROOT="+filepath.Join(root, "missing"))
	if err := runManagedRuntime(context.Background(), processes, badEnv, nil); err == nil {
		t.Fatal("missing managed root accepted")
	}
	if _, err := waitManagedBinding(context.Background(), worker.TLSDialConfig{}, worker.ControlSignerBindingQuery{}); err == nil {
		t.Fatal("invalid binding transport accepted")
	}
	profile, err := openManagedProfile(root)
	if err != nil {
		t.Fatal(err)
	}
	profile.close()
	if err = os.Mkdir(filepath.Join(root, "go-control", store.ControlDatabaseFilename), 0o700); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(root, "go-control/native-profile/signer-public-pin"), []byte(managedFixturePublicKey()), 0o600); err != nil {
		t.Fatal(err)
	}
	if err = runManagedRuntime(context.Background(), processes, env, io.Discard); err == nil {
		t.Fatal("invalid durable store admitted by launch")
	}
}
