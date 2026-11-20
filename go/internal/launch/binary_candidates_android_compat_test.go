//go:build linux && (android || deepseek_android)

package launch

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAndroidPackagedNativeRoleResolutionRetainsEveryRequiredProcess(t *testing.T) {
	dir := t.TempDir()
	roles := map[string]string{"deepseekd": "libdeepseek_control.so", "deepseek-worker": "libdeepseek_worker.so", "deepseek-gateway": "libdeepseek_gateway.so"}
	for name, filename := range roles {
		expected := filepath.Join(dir, filename)
		if err := os.WriteFile(expected, []byte("packaged-image-"+name), 0o700); err != nil {
			t.Fatal(err)
		}
		actual, err := resolveBinary(dir, name)
		if err != nil || actual != expected {
			t.Fatalf("packaged role %s: %q %v", name, actual, err)
		}
	}
	plan, err := ProductionPlanWithAddress(dir, t.TempDir(), t.TempDir(), "127.0.0.1:8000")
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.Processes) != len(roles) {
		t.Fatal("native process role omitted", plan.Processes)
	}
	for _, process := range plan.Processes {
		if filepath.Base(process.Path) != roles[process.Name] {
			t.Fatal("packaged role rebound", process)
		}
	}
	if err := os.Remove(filepath.Join(dir, roles["deepseek-worker"])); err != nil {
		t.Fatal(err)
	}
	if _, err := ProductionPlanWithAddress(dir, t.TempDir(), t.TempDir(), "127.0.0.1:8000"); err == nil {
		t.Fatal("missing packaged worker admitted")
	}
}

func TestAndroidPlatformOcrCredentialReachesOnlyGateway(t *testing.T) {
	t.Setenv("DEEPSEEK_ANDROID_PLATFORM_OCR_ADDR", "127.0.0.1:32123")
	t.Setenv("DEEPSEEK_ANDROID_PLATFORM_OCR_TOKEN", "test-platform-credential")
	env := productionEnv(t.TempDir(), t.TempDir(), "127.0.0.1:8090", "127.0.0.1:8000")
	for _, role := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway"} {
		child := productionChildEnvironment(role, env)
		found := 0
		for _, item := range child {
			if strings.HasPrefix(item, "DEEPSEEK_ANDROID_PLATFORM_OCR_") {
				found++
			}
		}
		want := 0
		if role == "deepseek-gateway" {
			want = 2
		}
		if found != want {
			t.Fatalf("OCR credential isolation for %s: %d", role, found)
		}
	}
}
