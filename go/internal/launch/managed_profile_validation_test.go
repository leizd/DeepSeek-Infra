//go:build !android && !deepseek_android

package launch

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// These exercise the validation helpers that guard managed-profile custody. Each
// one answers "may this path or value be trusted", and every refusal branch is the
// difference between reading a bounded, regular, owner-controlled file and reading
// whatever an attacker placed at that path.
//
// The tests are deliberately portable: they build real temporary directory trees
// and call the helpers directly, so they run on Windows and Linux alike rather than
// only in the CI container.

func TestReadManagedValueRefusesEmptyOversizedAndFailingReaders(t *testing.T) {
	value, err := readManagedValue(strings.NewReader("kept"), 16)
	if err != nil || string(value) != "kept" {
		t.Fatalf("a bounded value was refused: %q %v", value, err)
	}
	// Exactly at the limit is legal; one byte over is not.
	if _, err := readManagedValue(strings.NewReader(strings.Repeat("x", 16)), 16); err != nil {
		t.Fatalf("an exactly-at-limit value was refused: %v", err)
	}
	for name, reader := range map[string]io.Reader{
		"empty":    strings.NewReader(""),
		"oversize": strings.NewReader(strings.Repeat("x", 17)),
	} {
		if _, err := readManagedValue(reader, 16); !errors.Is(err, errManagedProfile) {
			t.Fatalf("%s: expected errManagedProfile, got %v", name, err)
		}
	}
	if _, err := readManagedValue(failingReader{}, 16); !errors.Is(err, errReadFailure) {
		t.Fatalf("a reader failure was not propagated: %v", err)
	}
}

func TestManagedFileRefusesNonRegularEmptyAndOversizedFiles(t *testing.T) {
	root := t.TempDir()
	good := filepath.Join(root, "good")
	if err := os.WriteFile(good, []byte("kept"), 0o600); err != nil {
		t.Fatal(err)
	}
	value, err := managedFile(good, 16)
	if err != nil || string(value) != "kept" {
		t.Fatalf("a regular bounded file was refused: %q %v", value, err)
	}

	empty := filepath.Join(root, "empty")
	if err := os.WriteFile(empty, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	oversized := filepath.Join(root, "oversized")
	if err := os.WriteFile(oversized, []byte(strings.Repeat("x", 32)), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{"empty": empty, "oversized": oversized} {
		if _, err := managedFile(path, 16); !errors.Is(err, errManagedProfile) {
			t.Fatalf("%s: expected errManagedProfile, got %v", name, err)
		}
	}
	if _, err := managedFile(filepath.Join(root, "missing"), 16); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("a missing path did not report ErrNotExist: %v", err)
	}
	// A directory is not a readable value even though it exists.
	if _, err := managedFile(root, 16); !errors.Is(err, errManagedProfile) {
		t.Fatalf("a directory was accepted as a managed value: %v", err)
	}
}

func TestManagedDirectoryCreatesAndRevalidatesTheDirectory(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "profile")
	if err := managedDirectory(path); err != nil {
		t.Fatalf("a fresh directory was refused: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil || !info.IsDir() {
		t.Fatalf("the directory was not created: %v", err)
	}
	// Idempotent on a second call.
	if err := managedDirectory(path); err != nil {
		t.Fatalf("an existing valid directory was refused: %v", err)
	}
	// A regular file where a directory belongs must be refused, not overwritten.
	filePath := filepath.Join(root, "not-a-directory")
	if err := os.WriteFile(filePath, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := managedDirectory(filePath); !errors.Is(err, errManagedProfile) {
		t.Fatalf("a regular file was accepted as a managed directory: %v", err)
	}
}

func TestInitiallyEmptyNativeProfileRecognisesOnlyTheFirstRunLayout(t *testing.T) {
	// The metadata directory lives beside the profile root, not inside it.
	base := t.TempDir()
	root := filepath.Join(base, "profile")
	metadata := filepath.Join(base, "metadata")

	// An absent root is not an empty profile: it cannot be judged.
	if initiallyEmptyNativeProfile(root, metadata) {
		t.Fatal("an absent root was reported as an empty profile")
	}

	// A first run may hold only the auth token outside a launcher-only go-control.
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "go-control", filepath.Base(metadata)), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(metadata, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(metadata, "launch.lock"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".auth-token"), []byte("token"), 0o600); err != nil {
		t.Fatal(err)
	}
	if !initiallyEmptyNativeProfile(root, metadata) {
		t.Fatal("a first-run layout was not recognised")
	}

	// Any other entry at the root means this is not a first run.
	if err := os.WriteFile(filepath.Join(root, "unexpected"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if initiallyEmptyNativeProfile(root, metadata) {
		t.Fatal("an extra root entry was treated as an empty profile")
	}
}

func TestInitiallyEmptyNativeProfileRejectsAnAuthTokenThatIsNotAFile(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "profile")
	metadata := filepath.Join(base, "metadata")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(metadata, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(metadata, "launch.lock"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A directory named .auth-token must not pass the regular-file check.
	if err := os.MkdirAll(filepath.Join(root, ".auth-token"), 0o700); err != nil {
		t.Fatal(err)
	}
	if initiallyEmptyNativeProfile(root, metadata) {
		t.Fatal("a non-file .auth-token was accepted")
	}
}

func TestInitiallyEmptyNativeProfileRejectsAPopulatedGoControl(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "profile")
	metadata := filepath.Join(base, "metadata")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(metadata, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(metadata, "launch.lock"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A go-control holding something other than launcher/<metadata> means the Go
	// control plane has already run, so custody may not be created over it.
	control := filepath.Join(root, "go-control")
	if err := os.MkdirAll(filepath.Join(control, "state"), 0o700); err != nil {
		t.Fatal(err)
	}
	if initiallyEmptyNativeProfile(root, metadata) {
		t.Fatal("a populated go-control was accepted")
	}
}

func TestManagedPathsEqualForOSFollowsThePlatformFold(t *testing.T) {
	// Windows compares case-insensitively and separators are normalised there; on
	// every other platform the comparison is exact after cleaning.
	if !managedPathsEqualForOS("/a/b", "/a/b", "linux") {
		t.Fatal("identical POSIX paths compared unequal")
	}
	if managedPathsEqualForOS("/a/b", "/a/B", "linux") {
		t.Fatal("POSIX paths compared case-insensitively")
	}
	if !managedPathsEqualForOS(`C:\A\B`, `C:\a\b`, "windows") {
		t.Fatal("Windows paths did not compare case-insensitively")
	}
	// The current-platform wrapper must agree with the platform it is running on.
	separator := string(filepath.Separator)
	path := separator + "a" + separator + "b"
	if !sameManagedPath(path, path) {
		t.Fatalf("sameManagedPath disagreed with itself on %s", runtime.GOOS)
	}
}

func TestLaunchEnvironmentValuePrefersTheLastAssignment(t *testing.T) {
	environment := []string{"A=1", "B=2", "A=3"}
	if got := launchEnvironmentValue(environment, "A"); got != "3" {
		t.Fatalf("got %q, want the last assignment 3", got)
	}
	if got := launchEnvironmentValue(environment, "C"); got != "" {
		t.Fatalf("a missing key returned %q", got)
	}
	if got := launchEnvironmentValue(nil, "A"); got != "" {
		t.Fatalf("an empty environment returned %q", got)
	}
}

func TestManagedOperatorConfigurationDetectsEveryOperatorKey(t *testing.T) {
	for _, key := range []string{
		"DEEPSEEKD_INTERNAL_RPC_X", "DEEPSEEK_GO_CONTROL_RPC_X", "DEEPSEEK_WORKER_TLS_X",
		"DEEPSEEK_WORKER_AUTHORITY_X", "DEEPSEEK_WORKER_CONTROL_SIGNER_X",
		"DEEPSEEK_WORKER_STATE_ROOT", "DEEPSEEK_WORKER_SERVICE_X", "DEEPSEEK_WORKER_TARGET",
		"DEEPSEEKD_INTERNAL_BEARER", "DEEPSEEK_INTERNAL_BEARER", "DEEPSEEKD_CONTROL_AUTHORITY",
		"DEEPSEEKD_MUTATION_SIGNER_KEY", "DEEPSEEKD_PROMOTION_SIGNER_KEY",
	} {
		if !managedOperatorConfiguration([]string{key + "=value"}) {
			t.Fatalf("%s was not recognised as operator configuration", key)
		}
	}
	for _, entry := range []string{"UNRELATED=1", "DEEPSEEK_WORKER_TLS", "PATH=/usr/bin"} {
		if managedOperatorConfiguration([]string{entry}) {
			t.Fatalf("%q was mistaken for operator configuration", entry)
		}
	}
	if managedOperatorConfiguration(nil) {
		t.Fatal("an empty environment reported operator configuration")
	}
}

var errReadFailure = errors.New("read failure")

type failingReader struct{}

func (failingReader) Read([]byte) (int, error) { return 0, errReadFailure }
