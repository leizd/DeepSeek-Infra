package launch

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func temporarySetting(environment []string, key string) (string, bool) {
	for _, entry := range environment {
		name, value, _ := strings.Cut(entry, "=")
		if name == key {
			return value, true
		}
	}
	return "", false
}

// A Go map populated with Getenv used to turn an absent TMPDIR into TMPDIR=.
// Rust then sees an empty path instead of its OS temporary directory, and the
// real native default-image worker exits before readiness. Keep this distinct
// from configuration values whose deliberate emptiness enforces a denial.
func TestProductionTemporaryDirectoriesRetainOSDefaultsAndExactOverrides(t *testing.T) {
	for _, key := range []string{"TEMP", "TMP", "TMPDIR"} {
		t.Run(key, func(t *testing.T) {
			t.Setenv(key, "restore-the-original-test-environment")
			if err := os.Unsetenv(key); err != nil {
				t.Fatal(err)
			}
			for _, state := range []string{"unset", "empty"} {
				if state == "empty" {
					t.Setenv(key, "")
				}
				environment := productionEnv(t.TempDir(), t.TempDir(), "127.0.0.1:8090", "127.0.0.1:8000")
				if _, exists := temporarySetting(environment, key); exists {
					t.Fatalf("%s %s became an explicit empty child override", state, key)
				}
				if value, exists := temporarySetting(environment, "DEEPSEEKD_SHADOW_STORE"); !exists || value != "" {
					t.Fatal("the explicit shadow-store denial was lost")
				}
			}
			path := filepath.Join(t.TempDir(), "exact temporary directory with spaces")
			if err := os.Mkdir(path, 0700); err != nil {
				t.Fatal(err)
			}
			t.Setenv(key, path)
			environment := productionEnv(t.TempDir(), t.TempDir(), "127.0.0.1:8090", "127.0.0.1:8000")
			for _, role := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway", "deepseek-desktop"} {
				value, exists := temporarySetting(productionChildEnvironment(role, environment), key)
				if !exists || value != path {
					t.Fatalf("%s lost or trimmed its explicit %s path", role, key)
				}
			}
			if value := os.Getenv(key); value != path {
				t.Fatal("planning changed the parent's temporary directory")
			}
		})
	}
}
