package launch

import (
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func managedFixturePublicKey() string {
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = 42
	}
	return base64.RawURLEncoding.EncodeToString(ed25519.NewKeyFromSeed(seed).Public().(ed25519.PublicKey))
} // offline process fixture only
func managedFixtureIdentity(root string) error {
	dir := filepath.Join(root, "custody")
	if _, err := os.Stat(dir); err == nil {
		return nil
	}
	if err := os.Mkdir(dir, 0o700); err != nil {
		return err
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	now := time.Now()
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour)}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &key.PublicKey, key)
	if err != nil {
		return err
	}
	cert := &x509.Certificate{SerialNumber: big.NewInt(2), DNSNames: []string{"deepseek-worker.local"}, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, NotBefore: ca.NotBefore, NotAfter: ca.NotAfter}
	certDER, err := x509.CreateCertificate(rand.Reader, cert, ca, &key.PublicKey, key)
	if err != nil {
		return err
	}
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return err
	}
	for name, bytes := range map[string][]byte{"transport-ca.pem": pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}), "worker-cert.pem": pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER}), "worker-key.pem": pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})} {
		if err = os.WriteFile(filepath.Join(dir, name), bytes, 0o600); err != nil {
			return err
		}
	}
	return nil
}

type managedFixtureBootstrap struct {
	actionv1.UnimplementedControlSignerBootstrapServer
}

func (managedFixtureBootstrap) GetBinding(ctx context.Context, q *actionv1.ControlSignerBindingRequest) (*actionv1.ControlSignerBindingResponse, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	values := md.Get("authorization")
	if len(values) != 1 || values[0] != "Bearer "+os.Getenv("DEEPSEEK_WORKER_SERVICE_BEARER") {
		return nil, status.Error(codes.Unauthenticated, "fixture credential denied")
	}
	fence, err := strconv.ParseUint(os.Getenv("DEEPSEEK_WORKER_MANAGED_FENCING_TOKEN"), 10, 64)
	public := managedFixturePublicKey()
	if err != nil || q.SchemaVersion != 1 || q.FleetId != "native-"+os.Getenv("DEEPSEEK_WORKER_MANAGED_PROFILE_ID") || q.Environment != "local" || q.FencingToken != fence || (q.ExpectedSignerPublicKey != "" && q.ExpectedSignerPublicKey != public) {
		return nil, status.Error(codes.FailedPrecondition, "fixture scope denied")
	}
	keyID, _ := store.SignerKeyIDForPublicKey(public)
	return &actionv1.ControlSignerBindingResponse{SchemaVersion: 1, SignerPublicKey: public, SignerKeyId: keyID, FleetId: q.FleetId, Environment: q.Environment, FencingToken: q.FencingToken}, nil
}
func runManagedFixtureHelper(role string) int {
	root := os.Getenv("DEEPSEEK_INFRA_ROOT")
	if os.Getenv("DEEPSEEK_WORKER_MANAGED_INITIALIZE") == "1" {
		if err := managedFixtureIdentity(os.Getenv("DEEPSEEK_WORKER_MANAGED_ROOT")); err != nil {
			return 91
		}
		return 0
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if role == "deepseek-worker" {
		custody := filepath.Join(os.Getenv("DEEPSEEK_WORKER_MANAGED_ROOT"), "custody")
		pair, err := tls.LoadX509KeyPair(filepath.Join(custody, "worker-cert.pem"), filepath.Join(custody, "worker-key.pem"))
		if err != nil {
			return 92
		}
		listener, err := net.Listen("tcp", os.Getenv("DEEPSEEK_WORKER_LISTEN"))
		if err != nil {
			return 93
		}
		defer listener.Close()
		server := grpc.NewServer(grpc.Creds(credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{pair}})))
		actionv1.RegisterControlSignerBootstrapServer(server, managedFixtureBootstrap{})
		go func() { <-ctx.Done(); server.Stop() }()
		_ = server.Serve(listener)
		return 0
	}
	if role == "deepseekd" {
		for _, name := range []string{"DEEPSEEK_WORKER_MANAGED_ROOT", "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE", "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE", "DEEPSEEK_WORKER_TLS_KEY_FILE", "DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE"} {
			if _, exists := os.LookupEnv(name); exists {
				return 94
			}
		}
		if os.Getenv("DEEPSEEKD_MUTATION_SIGNER_KEY") != managedFixturePublicKey() {
			return 95
		}
		control, err := store.OpenControl(store.OpenOptions{Path: os.Getenv("DEEPSEEKD_PRODUCTION_STORE"), Owner: os.Getenv("DEEPSEEKD_OWNER"), LeaseSeconds: 1})
		if err != nil {
			return 96
		}
		defer control.Close()
		expected, err := strconv.ParseInt(os.Getenv("DEEPSEEKD_EXPECTED_WRITER_FENCE"), 10, 64)
		if err != nil || control.Writer().FencingToken != expected {
			return 97
		}
		if err = os.WriteFile(filepath.Join(root, "fixture-control-ready"), []byte(strconv.FormatInt(expected, 10)), 0o600); err != nil {
			return 98
		}
		listener, err := net.Listen("tcp", os.Getenv("DEEPSEEKD_LISTEN"))
		if err != nil {
			return 101
		}
		defer listener.Close()
		mux := http.NewServeMux()
		mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
			lease := control.Writer()
			w.Header().Set(config.ControlWriterFenceHeader, strconv.FormatInt(lease.FencingToken, 10))
			w.Header().Set(config.ControlWriterOwnerHeader, lease.OwnerInstanceID)
			if os.Getenv("DEEPSEEK_MANAGED_FIXTURE_DELAY_CONTROL") == "1" {
				_ = os.WriteFile(filepath.Join(root, "fixture-readiness-probed"), []byte("observed"), 0o600)
				if _, err := os.Stat(filepath.Join(root, "fixture-release-control")); err != nil {
					w.WriteHeader(http.StatusServiceUnavailable)
					return
				}
			}
			w.WriteHeader(http.StatusOK)
		})
		server := &http.Server{Handler: mux, ReadHeaderTimeout: time.Second}
		go func() { <-ctx.Done(); _ = server.Close() }()
		_ = server.Serve(listener)
	} else if role == "deepseek-gateway" {
		if err := os.WriteFile(filepath.Join(root, "fixture-gateway-ready"), []byte("ready"), 0o600); err != nil {
			return 99
		}
	} else {
		return 100
	}
	<-ctx.Done()
	return 0
}
func TestManagedNativeStartupUsesAuthenticatedBindingAndRetainsWriterFence(t *testing.T) {
	root := t.TempDir()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, expected := range []int64{1, 2} {
		if expected == 2 {
			time.Sleep(1100 * time.Millisecond)
		} // Direct Android-tagged host children may be killed before deferred close.

		for _, name := range []string{"fixture-control-ready", "fixture-gateway-ready"} {
			_ = os.Remove(filepath.Join(root, name))
		}
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		address := listener.Addr().String()
		_ = listener.Close()
		env := []string{managedRequired + "=1", "DEEPSEEK_RUNTIME_MODE=python_disabled", "DEEPSEEKD_MODE=authoritative", "DEEPSEEK_INFRA_ROOT=" + root, "DEEPSEEKD_PRODUCTION_STORE=" + filepath.Join(root, "go-control"), "DEEPSEEK_WORKER_LISTEN=" + address, "DEEPSEEK_LAUNCH_HELPER=1"}
		env = managedFixtureControlEnvironment(t, env)
		processes := []Process{}
		for _, role := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway"} {
			processes = append(processes, Process{Name: role, Path: executable, Env: append(append([]string(nil), env...), "DEEPSEEK_MANAGED_FIXTURE_ROLE="+role)})
		}
		ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
		done := make(chan error, 1)
		go func() { done <- Run(ctx, processes, env, io.Discard) }()
		deadline := time.NewTimer(20 * time.Second)
		ticker := time.NewTicker(25 * time.Millisecond)
		ready := false
		for !ready {
			select {
			case err := <-done:
				cancel()
				ticker.Stop()
				deadline.Stop()
				t.Fatalf("managed process stopped before public binding: %v", err)
			case <-deadline.C:
				cancel()
				ticker.Stop()
				<-done
				t.Fatal("managed process did not become ready")
			case <-ticker.C:
				raw, readErr := os.ReadFile(filepath.Join(root, "fixture-control-ready"))
				_, gatewayErr := os.Stat(filepath.Join(root, "fixture-gateway-ready"))
				if readErr == nil && gatewayErr == nil {
					if string(raw) != strconv.FormatInt(expected, 10) {
						cancel()
						t.Fatal("writer fence changed")
					}
					ready = true
				}
			}
		}
		ticker.Stop()
		deadline.Stop()
		cancel()
		if err = <-done; err != nil {
			t.Fatal(err)
		}
		pin, err := managedFile(filepath.Join(root, "go-control/native-profile/signer-public-pin"), 4096)
		if err != nil || string(pin) != managedFixturePublicKey() {
			t.Fatal("managed public pin was not retained")
		}
		next, err := store.NextWriterFencingToken(filepath.Join(root, "go-control"))
		if err != nil || next != expected+1 {
			t.Fatalf("writer preview: %d %v", next, err)
		}
	}
}
func TestManagedProfileRequiresOriginalMetadataAndExclusiveOwner(t *testing.T) {
	root := t.TempDir()
	profile, err := openManagedProfile(root)
	if err != nil {
		t.Fatal(err)
	}
	id := profile.id
	if _, err = openManagedProfile(root); err == nil {
		t.Fatal("second owner admitted")
	}
	profile.close()
	profile, err = openManagedProfile(root)
	if err != nil || profile.id != id {
		t.Fatal("profile identity changed")
	}
	profile.close()
	if err = os.WriteFile(filepath.Join(root, "go-control/native-profile/profile-id"), []byte(strings.Repeat("A", 32)), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err = openManagedProfile(root); err == nil {
		t.Fatal("malformed profile admitted")
	}
	for _, legacy := range []string{".memory", ".projects", "go-control/control.db", "rust-worker"} {
		root := t.TempDir()
		path := filepath.Join(root, filepath.FromSlash(legacy))
		if err = os.MkdirAll(path, 0o700); err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(filepath.Join(path, "retained"), []byte("preserve original data"), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err = openManagedProfile(root); err == nil {
			t.Fatal("legacy profile adopted")
		}
		raw, err := os.ReadFile(filepath.Join(path, "retained"))
		if err != nil || string(raw) != "preserve original data" {
			t.Fatal("legacy bytes changed")
		}
	}
	for _, root := range []string{"", "relative"} {
		if _, err = openManagedProfile(root); err == nil {
			t.Fatal("invalid root admitted")
		}
	}
}
func TestManagedProfileNeverRecreatesLostCustodyOrGoState(t *testing.T) {
	for _, missing := range []string{"database", "pin", "custody"} {
		root := t.TempDir()
		profile, err := openManagedProfile(root)
		if err != nil {
			t.Fatal(err)
		}
		profile.close()
		if missing != "database" {
			control, err := store.OpenControl(store.OpenOptions{Path: filepath.Join(root, "go-control"), Owner: "offline-fixture"})
			if err != nil {
				t.Fatal(err)
			}
			if err = control.Close(); err != nil {
				t.Fatal(err)
			}
		}
		if missing != "pin" {
			if err = os.WriteFile(filepath.Join(root, "go-control/native-profile/signer-public-pin"), []byte(managedFixturePublicKey()), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		if missing == "custody" {
			if err = os.Remove(filepath.Join(root, "rust-worker")); err != nil {
				t.Fatal(err)
			}
		}
		if profile, err = openManagedProfile(root); err == nil {
			profile.close()
			t.Fatalf("missing %s was accepted", missing)
		}
	}
}
func TestManagedGoTransportIdentityIsBoundToOwnServer(t *testing.T) {
	now := time.Now().UTC()
	ca, cert, key, err := managedGoIdentity(now)
	if err != nil {
		t.Fatal(err)
	}
	pair, err := tls.X509KeyPair(cert, key)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		t.Fatal("missing transport root")
	}
	if _, err = leaf.Verify(x509.VerifyOptions{Roots: roots, DNSName: "deepseek-go-control.local", CurrentTime: now}); err != nil {
		t.Fatal(err)
	}
	if _, err = leaf.Verify(x509.VerifyOptions{Roots: roots, DNSName: "foreign.local", CurrentTime: now}); err == nil {
		t.Fatal("foreign identity accepted")
	}
}
func TestManagedRoleScopesAndExplicitOperatorProvisioning(t *testing.T) {
	env := []string{"DEEPSEEK_WORKER_MANAGED_ROOT=/private-rust", "DEEPSEEK_WORKER_TLS_KEY_FILE=/private-rust/key", "DEEPSEEK_WORKER_TLS_CERT_FILE=/public-rust/cert", "DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE=/private-rust/client-key", "DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE=/private-go/key", "DEEPSEEKD_EXPECTED_WRITER_FENCE=2"}
	for _, role := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway", "deepseek-desktop"} {
		values := productionChildEnvironment(role, env)
		if role != "deepseek-worker" && (launchEnvironmentValue(values, "DEEPSEEK_WORKER_MANAGED_ROOT") != "" || launchEnvironmentValue(values, "DEEPSEEK_WORKER_TLS_KEY_FILE") != "") {
			t.Fatal("Rust custody reached another role")
		}
		if role != "deepseek-worker" && role != "deepseek-gateway" && launchEnvironmentValue(values, "DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE") != "" {
			t.Fatal("Rust TLS client key reached Go")
		}
		if role != "deepseekd" && launchEnvironmentValue(values, "DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE") != "" {
			t.Fatal("Go key reached another role")
		}
	}
	for _, key := range []string{"DEEPSEEK_WORKER_SERVICE_ROLE", "DEEPSEEK_WORKER_TARGET", "DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", "DEEPSEEKD_INTERNAL_BEARER", "DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE", "DEEPSEEK_GO_CONTROL_RPC_URL"} {
		if !managedOperatorConfiguration([]string{key + "="}) {
			t.Fatal("explicit operator setting overridden")
		}
	}
	if managedOperatorConfiguration([]string{"AUTH_DISABLED=1", "DEEPSEEK_API_KEY=offline-fixture"}) {
		t.Fatal("public configuration disabled managed startup")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, process := range []Process{{Name: "other", Path: "/tmp/worker"}, {Name: "deepseek-worker", Path: "python"}} {
		if err := runManagedInitializer(ctx, process, nil, io.Discard); !errors.Is(err, errManagedProfile) {
			t.Fatal("invalid initializer accepted")
		}
	}
	if err := Run(ctx, nil, []string{managedRequired + "=1"}, io.Discard); !errors.Is(err, errManagedProfile) {
		t.Fatal("invalid managed process plan accepted")
	}
}

func TestManagedMissingKeysNeverTriggerInitializer(t *testing.T) {
	root := t.TempDir()
	profile, err := openManagedProfile(root)
	if err != nil {
		t.Fatal(err)
	}
	profile.close()
	control, err := store.OpenControl(store.OpenOptions{Path: filepath.Join(root, "go-control"), Owner: "offline-existing-profile"})
	if err != nil {
		t.Fatal(err)
	}
	if err = control.Close(); err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(filepath.Join(root, "go-control/native-profile/signer-public-pin"), []byte(managedFixturePublicKey()), 0o600); err != nil {
		t.Fatal(err)
	}
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	env := []string{managedRequired + "=1", "DEEPSEEK_RUNTIME_MODE=python_disabled", "DEEPSEEKD_MODE=authoritative", "DEEPSEEK_INFRA_ROOT=" + root, "DEEPSEEK_WORKER_LISTEN=127.0.0.1:50052", "DEEPSEEK_LAUNCH_HELPER=1"}
	env = managedFixtureControlEnvironment(t, env)
	processes := []Process{}
	for _, role := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway"} {
		processes = append(processes, Process{Name: role, Path: executable, Env: append(append([]string(nil), env...), "DEEPSEEK_MANAGED_FIXTURE_ROLE="+role)})
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err = Run(ctx, processes, env, io.Discard); err == nil {
		t.Fatal("lost custody admitted")
	}
	files, err := os.ReadDir(filepath.Join(root, "rust-worker"))
	if err != nil || len(files) != 0 {
		t.Fatal("existing database triggered replacement of lost keys")
	}
}

func TestManagedProfileRejectsRedirectedPrivateDirectories(t *testing.T) {
	root := t.TempDir()
	other := t.TempDir()
	link := filepath.Join(root, "redirected")
	if err := os.Symlink(other, link); err != nil {
		t.Fatal(err)
	}
	if err := managedDirectory(filepath.Join(link, "private")); err == nil {
		t.Fatal("redirected ancestor accepted")
	}
	if _, err := os.Stat(filepath.Join(other, "private")); !os.IsNotExist(err) {
		t.Fatal("redirected directory was created")
	}
	if _, err := openManagedProfile(link); err == nil {
		t.Fatal("redirected data root accepted")
	}
	if err := os.WriteFile(filepath.Join(root, "file"), []byte("retain"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := managedDirectory(filepath.Join(root, "file")); err == nil {
		t.Fatal("file admitted as private directory")
	}
	path := filepath.Join(root, "public-pin")
	if err := publishManagedValue(path, []byte("first")); err != nil {
		t.Fatal(err)
	}
	if err := publishManagedValue(path, []byte("second")); err == nil {
		t.Fatal("retained value overwritten")
	}
	if err := publishManagedValue(path, []byte("first")); err != nil {
		t.Fatal(err)
	}
	if _, err := managedFile(path, 1); err == nil {
		t.Fatal("oversize metadata accepted")
	}
	if _, err := managedFile(root, 100); err == nil {
		t.Fatal("directory admitted as metadata")
	}
}
