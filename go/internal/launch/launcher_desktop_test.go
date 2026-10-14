package launch

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestLauncherWindowReceivesOnlyItsEphemeralUISession(t *testing.T) {
	bin := t.TempDir()
	name := "deepseek-desktop"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	if err := os.WriteFile(filepath.Join(bin, name), []byte("isolated metadata-only fixture"), 0700); err != nil {
		t.Fatal(err)
	}
	plan, err := WithLauncherDesktop(Plan{GatewayURL: "http://127.0.0.1:18123/launcher", Env: []string{"AUTH_TOKEN=owned-ui-token", "DEEPSEEK_API_KEY=provider-secret", "TAVILY_API_KEY=search-secret", "DEEPSEEKD_INTERNAL_BEARER=control-secret"}}, bin, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.Processes) != 1 {
		t.Fatal("opening the launcher implicitly started business processes")
	}
	ui := plan.Processes[0]
	joined := strings.Join(ui.Env, "\n")
	if !strings.Contains(joined, "DEEPSEEK_DESKTOP_KIND=launcher") || !strings.Contains(joined, "/launcher?desktop=1&token=owned-ui-token") || !strings.Contains(joined, ".launcher-webview") {
		t.Fatal("private launcher window lost its distinct readiness and profile")
	}
	for _, secret := range []string{"provider-secret", "search-secret", "control-secret"} {
		if strings.Contains(joined, secret) {
			t.Fatal("a business credential entered the platform host")
		}
	}
}
