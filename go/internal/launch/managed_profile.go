package launch

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"io"
	"math/big"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const managedRequired = "DEEPSEEK_NATIVE_MANAGED_BOOTSTRAP"

var errManagedProfile = errors.New("native managed profile is incomplete, foreign or already active")

type managedProfile struct {
	root, metadata, rust, id, pin string
	lock                          *os.File
}

func (profile *managedProfile) close() {
	if profile.lock != nil {
		_ = profile.lock.Close()
	}
}
func launchEnvironmentValue(environment []string, key string) string {
	for i := len(environment) - 1; i >= 0; i-- {
		name, value, _ := strings.Cut(environment[i], "=")
		if name == key {
			return value
		}
	}
	return ""
}
func managedOperatorConfiguration(environment []string) bool {
	for _, entry := range environment {
		key, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(key, "DEEPSEEKD_INTERNAL_RPC_") || strings.HasPrefix(key, "DEEPSEEK_GO_CONTROL_RPC_") || strings.HasPrefix(key, "DEEPSEEK_WORKER_TLS_") || strings.HasPrefix(key, "DEEPSEEK_WORKER_AUTHORITY_") || strings.HasPrefix(key, "DEEPSEEK_WORKER_CONTROL_SIGNER_") || key == "DEEPSEEK_WORKER_STATE_ROOT" || strings.HasPrefix(key, "DEEPSEEK_WORKER_SERVICE_") || key == "DEEPSEEK_WORKER_TARGET" || key == "DEEPSEEKD_INTERNAL_BEARER" || key == "DEEPSEEK_INTERNAL_BEARER" || key == "DEEPSEEKD_CONTROL_AUTHORITY" || key == "DEEPSEEKD_MUTATION_SIGNER_KEY" || key == "DEEPSEEKD_PROMOTION_SIGNER_KEY" {
			return true
		}
	}
	return false
}
func managedFile(path string, limit int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > limit {
		return nil, errManagedProfile
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	return readManagedValue(file, limit)
}

func readManagedValue(file io.Reader, limit int64) ([]byte, error) {
	bytes, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, err
	}
	if len(bytes) == 0 || int64(len(bytes)) > limit {
		return nil, errManagedProfile
	}
	return bytes, nil
}
func sameManagedPath(a, b string) bool { return managedPathsEqualForOS(a, b, runtime.GOOS) }
func managedPathsEqualForOS(a, b, goos string) bool {
	if goos == "windows" {
		return strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
	}
	return filepath.Clean(a) == filepath.Clean(b)
}
func managedDirectory(path string) error {
	// Resolve the nearest existing ancestor before creating/chmodding anything.
	// A preexisting junction or symlink must not redirect a private directory.
	ancestor := filepath.Clean(path)
	for {
		info, err := os.Lstat(ancestor)
		if err == nil {
			if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
				return errManagedProfile
			}
			resolved, err := filepath.EvalSymlinks(ancestor)
			if err != nil || !sameManagedPath(resolved, ancestor) {
				return errManagedProfile
			}
			break
		}
		if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		parent := filepath.Dir(ancestor)
		if parent == ancestor {
			return errManagedProfile
		}
		ancestor = parent
	}
	if info, err := os.Lstat(path); err == nil && (!info.IsDir() || info.Mode()&os.ModeSymlink != 0) {
		return errManagedProfile
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := secureManagedDirectory(path); err != nil {
		return err
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil || !sameManagedPath(resolved, path) {
		return errManagedProfile
	}
	return nil
}
func publishManagedValue(path string, value []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".native-pending-")
	if err != nil {
		return err
	}
	pending := file.Name()
	defer os.Remove(pending)
	_, writeErr := file.Write(value)
	syncErr := file.Sync()
	closeErr := file.Close()
	if err = errors.Join(writeErr, syncErr, closeErr); err != nil {
		return err
	}
	if err = os.Link(pending, path); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return err
		}
		prior, readErr := managedFile(path, 4096)
		if readErr != nil || string(prior) != string(value) {
			return errManagedProfile
		}
	}
	return nil
}
func randomManagedHex(bytes int) (string, error) {
	value := make([]byte, bytes)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	return hex.EncodeToString(value), nil
}
func initiallyEmptyNativeProfile(root, metadata string) bool {
	entries, err := os.ReadDir(root)
	if err != nil {
		return false
	}
	for _, entry := range entries {
		if entry.Name() == ".auth-token" {
			if !entry.Type().IsRegular() {
				return false
			}
			continue
		}
		if entry.Name() != "go-control" || !entry.IsDir() {
			return false
		}
		children, err := os.ReadDir(filepath.Join(root, "go-control"))
		if err != nil {
			return false
		}
		for _, child := range children {
			if (child.Name() != "launcher" && child.Name() != filepath.Base(metadata)) || !child.IsDir() {
				return false
			}
		}
	}
	entries, err = os.ReadDir(metadata)
	if err != nil {
		return false
	}
	for _, entry := range entries {
		if entry.Name() != "launch.lock" || !entry.Type().IsRegular() {
			return false
		}
	}
	return true
}
func openManagedProfile(root string) (*managedProfile, error) {
	if root == "" || !filepath.IsAbs(root) {
		return nil, errManagedProfile
	}
	root = filepath.Clean(root)
	resolved, err := filepath.EvalSymlinks(root)
	if err != nil || !sameManagedPath(resolved, root) {
		return nil, errManagedProfile
	}
	profile := &managedProfile{root: root, metadata: filepath.Join(root, "go-control", "native-profile"), rust: filepath.Join(root, "rust-worker")}
	if err = managedDirectory(profile.metadata); err != nil {
		return nil, err
	}
	path := filepath.Join(profile.metadata, "launch.lock")
	if info, e := os.Lstat(path); e == nil && !info.Mode().IsRegular() {
		return nil, errManagedProfile
	}
	profile.lock, err = os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err = lockManagedProfile(profile.lock); err != nil {
		profile.close()
		return nil, errManagedProfile
	}
	success := false
	defer func() {
		if !success {
			profile.close()
		}
	}()
	idPath := filepath.Join(profile.metadata, "profile-id")
	id, err := managedFile(idPath, 64)
	if errors.Is(err, os.ErrNotExist) {
		if !initiallyEmptyNativeProfile(root, profile.metadata) {
			return nil, errManagedProfile
		}
		profile.id, err = randomManagedHex(16)
		if err != nil {
			return nil, err
		}
		if err = publishManagedValue(idPath, []byte(profile.id)); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	} else {
		profile.id = string(id)
	}
	if len(profile.id) != 32 || strings.ToLower(profile.id) != profile.id {
		return nil, errManagedProfile
	}
	if _, err = hex.DecodeString(profile.id); err != nil {
		return nil, errManagedProfile
	}
	pin, err := managedFile(filepath.Join(profile.metadata, "signer-public-pin"), 4096)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	profile.pin = string(pin)
	if profile.pin != "" {
		if _, err = store.SignerKeyIDForPublicKey(profile.pin); err != nil {
			return nil, errManagedProfile
		}
	}
	_, dbErr := os.Lstat(filepath.Join(root, "go-control", store.ControlDatabaseFilename))
	if (profile.pin != "") != (dbErr == nil) {
		return nil, errManagedProfile
	}
	if dbErr != nil && !errors.Is(dbErr, os.ErrNotExist) {
		return nil, dbErr
	}
	if _, err = os.Lstat(profile.rust); errors.Is(err, os.ErrNotExist) && profile.pin != "" {
		return nil, errManagedProfile
	}
	if err = managedDirectory(profile.rust); err != nil {
		return nil, err
	}
	success = true
	return profile, nil
}
func managedGoIdentity(now time.Time) ([]byte, []byte, []byte, error) {
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, err
	}
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "DeepSeek local Go transport"}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(2 * time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, err
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, nil, err
	}
	leaf := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "deepseek-go-control.local"}, DNSNames: []string{"deepseek-go-control.local"}, NotBefore: ca.NotBefore, NotAfter: ca.NotAfter, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	leafDER, err := x509.CreateCertificate(rand.Reader, leaf, ca, &key.PublicKey, caKey)
	if err != nil {
		return nil, nil, nil, err
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, nil, nil, err
	}
	rootPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leafDER})
	certPEM = append(certPEM, rootPEM...)
	return rootPEM, certPEM, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}), nil
}
func runManagedInitializer(ctx context.Context, process Process, environment []string, output io.Writer) error {
	return runManagedInitializerWithFence(ctx, process, environment, output, func() (supervisorFence, error) { return newProcessFence() })
}

func runManagedInitializerWithFence(ctx context.Context, process Process, environment []string, output io.Writer, createFence func() (supervisorFence, error)) error {
	if process.Name != "deepseek-worker" || !filepath.IsAbs(process.Path) || legacyCommand(process.Path) {
		return errManagedProfile
	}
	ctx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	fence, err := createFence()
	if err != nil {
		return err
	}
	defer fence.close()
	prepared, err := fence.prepare(process, environment)
	if err != nil {
		return err
	}
	cmd := exec.CommandContext(ctx, prepared.path, prepared.args[1:]...)
	cmd.SysProcAttr = fence.attributes()
	cmd.Env = prepared.env
	cmd.ExtraFiles = prepared.files
	cmd.Stdout = output
	cmd.Stderr = output
	if prepared.cancel != nil {
		cmd.Cancel = prepared.cancel
	}
	if err = cmd.Start(); err != nil {
		return err
	}
	if err = fence.admit(cmd.Process.Pid); err != nil {
		cancel()
		_ = cmd.Wait()
		return err
	}
	if err = cmd.Wait(); err != nil {
		return errors.New("Rust managed custody initialization failed")
	}
	return nil
}
func waitManagedBinding(ctx context.Context, cfg worker.TLSDialConfig, query worker.ControlSignerBindingQuery) (worker.ControlSignerBinding, error) {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	client, err := worker.DialTLS(cfg)
	if err != nil {
		return worker.ControlSignerBinding{}, err
	}
	defer client.Close()
	for {
		request, stop := context.WithTimeout(ctx, time.Second)
		binding, err := client.ReadControlSignerBinding(request, query)
		stop()
		if err == nil {
			return binding, nil
		}
		if status.Code(err) != codes.Unavailable && status.Code(err) != codes.DeadlineExceeded {
			return worker.ControlSignerBinding{}, err
		}
		timer := time.NewTimer(50 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return worker.ControlSignerBinding{}, errors.New("managed Rust signer did not become ready")
		case <-timer.C:
		}
	}
}

// Each launch owns its primitives. Production always uses the native defaults;
// tests inject storage/transport failures without global hooks or runtime flags.
type managedRuntimeOperations struct {
	initialize       func(context.Context, Process, []string, io.Writer) error
	identity         func(time.Time) ([]byte, []byte, []byte, error)
	random           func(int) (string, error)
	session          func(string, string) (string, error)
	privateDirectory func(string) error
	writeFile        func(string, []byte, os.FileMode) error
	binding          func(context.Context, worker.TLSDialConfig, worker.ControlSignerBindingQuery) (worker.ControlSignerBinding, error)
	publish          func(string, []byte) error
}

func nativeManagedOperations() managedRuntimeOperations {
	return managedRuntimeOperations{
		initialize: runManagedInitializer, identity: managedGoIdentity, random: randomManagedHex,
		session: os.MkdirTemp, privateDirectory: managedDirectory, writeFile: os.WriteFile,
		binding: waitManagedBinding, publish: publishManagedValue,
	}
}

func runManagedRuntime(ctx context.Context, processes []Process, environment []string, output io.Writer) error {
	return runManagedRuntimeWith(ctx, processes, environment, output, nativeManagedOperations())
}
func runManagedRuntimeWith(ctx context.Context, processes []Process, environment []string, output io.Writer, operations managedRuntimeOperations) error {
	seen := map[string]bool{}
	for _, process := range processes {
		if seen[process.Name] || !filepath.IsAbs(process.Path) || legacyCommand(process.Path) {
			return errManagedProfile
		}
		switch process.Name {
		case "deepseekd", "deepseek-worker", "deepseek-gateway", "deepseek-desktop":
		default:
			return errManagedProfile
		}
		seen[process.Name] = true
	}
	if !seen["deepseekd"] || !seen["deepseek-worker"] || !seen["deepseek-gateway"] || launchEnvironmentValue(environment, "DEEPSEEK_RUNTIME_MODE") != "python_disabled" || launchEnvironmentValue(environment, "DEEPSEEKD_MODE") != "authoritative" {
		return errManagedProfile
	}
	if output == nil {
		output = os.Stdout
	}
	profile, err := openManagedProfile(launchEnvironmentValue(environment, "DEEPSEEK_INFRA_ROOT"))
	if err != nil {
		return err
	}
	defer profile.close()
	fence, err := store.NextWriterFencingToken(filepath.Join(profile.root, "go-control"))
	if err != nil {
		return err
	}
	plan := Plan{Processes: append([]Process(nil), processes...), Env: append([]string(nil), environment...)}
	workerIndex := -1
	for i, process := range plan.Processes {
		if process.Name == "deepseek-worker" {
			if workerIndex != -1 {
				return errManagedProfile
			}
			workerIndex = i
		}
	}
	if workerIndex < 0 {
		return errManagedProfile
	}
	base := []string{"DEEPSEEK_WORKER_MANAGED_ROOT=" + profile.rust, "DEEPSEEK_WORKER_MANAGED_PROFILE_ID=" + profile.id, "DEEPSEEK_WORKER_MANAGED_FENCING_TOKEN=" + strconv.FormatInt(fence, 10)}
	initializer := withProductionEnvironment(plan, append(append([]string(nil), base...), "DEEPSEEK_WORKER_MANAGED_INITIALIZE=1"))
	// An existing Go database/public pin can never trigger custody creation.
	// In particular, missing Rust keys after a crash stay a hard failure.
	if profile.pin == "" {
		if err = operations.initialize(ctx, initializer.Processes[workerIndex], initializer.Processes[workerIndex].Env, output); err != nil {
			return err
		}
	}
	// Go reads only the public Rust transport certificate; no Rust key, bundle,
	// passphrase, signing seed or private-key digest enters this process.
	trustPath := filepath.Join(profile.rust, "custody", "transport-ca.pem")
	publicCA, err := managedFile(trustPath, 128*1024)
	if err != nil {
		return err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(publicCA) {
		return errManagedProfile
	}
	session, err := operations.session(profile.metadata, "transport-")
	if err != nil {
		return err
	}
	files := []string{"go-ca.pem", "go-server.pem", "go-server-key.pem"}
	defer func() {
		for _, name := range files {
			_ = os.Remove(filepath.Join(session, name))
		}
		_ = os.Remove(session)
	}()
	if err = operations.privateDirectory(session); err != nil {
		return err
	}
	now := time.Now().UTC().Truncate(time.Second)
	ca, cert, key, err := operations.identity(now)
	if err != nil {
		return err
	}
	for i, bytes := range [][]byte{ca, cert, key} {
		if err = operations.writeFile(filepath.Join(session, files[i]), bytes, 0o600); err != nil {
			return err
		}
	}
	bearer, err := operations.random(32)
	if err != nil {
		return err
	}
	controlBearer, err := operations.random(32)
	if err != nil {
		return err
	}
	expires := now.Add(55 * time.Minute)
	values := append(base,
		"DEEPSEEKD_EXPECTED_WRITER_FENCE="+strconv.FormatInt(fence, 10),
		"DEEPSEEKD_INTERNAL_BEARER="+controlBearer, "DEEPSEEK_INTERNAL_BEARER="+controlBearer,
		"DEEPSEEKD_INTERNAL_RPC_LISTEN=127.0.0.1:8091", "DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE="+trustPath,
		"DEEPSEEKD_INTERNAL_RPC_TLS_CERT_FILE="+filepath.Join(session, "go-server.pem"), "DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE="+filepath.Join(session, "go-server-key.pem"),
		"DEEPSEEK_GO_CONTROL_RPC_URL=https://127.0.0.1:8091", "DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE="+filepath.Join(session, "go-ca.pem"),
		"DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE="+filepath.Join(profile.rust, "custody", "control-client.pem"), "DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE="+filepath.Join(profile.rust, "custody", "control-client-key.pem"),
		"DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME=deepseek-go-control.local",
		"DEEPSEEK_WORKER_TARGET="+launchEnvironmentValue(environment, "DEEPSEEK_WORKER_LISTEN"), "DEEPSEEK_WORKER_TLS_CA_FILE="+trustPath,
		"DEEPSEEK_WORKER_TLS_SERVER_NAME=deepseek-worker.local", "DEEPSEEK_WORKER_SERVICE_BEARER="+bearer, "DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT="+expires.Format("2006-01-02T15:04:05Z"),
		"DEEPSEEK_WORKER_SERVICE_NAME=go-control-plane", "DEEPSEEK_WORKER_SERVICE_ROLE=controller")
	plan = withProductionEnvironment(plan, values)
	if err = validatePrivateControlDeployment(plan.Env); err != nil {
		return err
	}
	first := plan.Processes[workerIndex]
	plan.Processes = append(plan.Processes[:workerIndex], plan.Processes[workerIndex+1:]...)
	plan.Processes = append([]Process{first}, plan.Processes...)
	query := worker.ControlSignerBindingQuery{FleetID: "native-" + profile.id, Environment: "local", PublicKey: profile.pin, FencingToken: fence}
	dial := worker.TLSDialConfig{Target: launchEnvironmentValue(environment, "DEEPSEEK_WORKER_LISTEN"), TrustRootFile: trustPath, ServerName: "deepseek-worker.local", BearerToken: bearer, ExpiresAt: expires}
	afterStart := func(index int) error {
		if index != 0 {
			return nil
		}
		binding, err := operations.binding(ctx, dial, query)
		if err != nil {
			return err
		}
		if err = operations.publish(filepath.Join(profile.metadata, "signer-public-pin"), []byte(binding.PublicKey)); err != nil {
			return err
		}
		updated := withProductionEnvironment(plan, []string{"DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=" + binding.PublicKey, "DEEPSEEKD_MUTATION_SIGNER_KEY=" + binding.PublicKey, "DEEPSEEKD_PROMOTION_SIGNER_KEY=" + binding.PublicKey, "DEEPSEEKD_FLEET_ID=" + binding.FleetID, "DEEPSEEKD_ENVIRONMENT=" + binding.Environment})
		for i := range plan.Processes {
			plan.Processes[i].Env = updated.Processes[i].Env
		}
		return nil
	}
	return runWithFenceAfterStart(ctx, plan.Processes, plan.Env, output, func() (supervisorFence, error) { return newProcessFence() }, afterStart)
}
