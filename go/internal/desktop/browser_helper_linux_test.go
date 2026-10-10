//go:build linux && !android

package desktop

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The helper is this test's native Go binary, never a shell or a real browser.
// It proves the OS-hook process boundary without affecting a user's session.
func TestMain(m *testing.M) {
	if filepath.Base(os.Args[0]) == "termux-open-url" {
		if os.Getenv("TERMUX__USER_ID") == "88" {
			time.Sleep(30 * time.Second)
			os.Exit(90)
		}
		report, _ := json.Marshal(map[string]any{"args": os.Args[1:], "environment": os.Environ()})
		if err := os.WriteFile(filepath.Join(os.Getenv("TMPDIR"), "native-browser-hook.json"), report, 0o600); err != nil {
			os.Exit(91)
		}
		if os.Getenv("TERMUX__USER_ID") == "77" {
			os.Exit(7)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestTermuxHookHasOneURLNoBackendSecretsAndSettlesNativeHelper(t *testing.T) {
	root := t.TempDir()
	self, err := os.ReadFile(os.Args[0])
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "termux-open-url"), self, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", root)
	t.Setenv("TMPDIR", root)
	t.Setenv("TERMUX_VERSION", "0.118")
	t.Setenv("TERMUX__USER_ID", "0")
	t.Setenv("DEEPSEEK_API_KEY", "private-provider")
	t.Setenv("AUTH_TOKEN", "private-token")
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", "private-authority")
	raw := "http://127.0.0.1:8123/?token=fixture"
	if err := OpenBrowser(context.Background(), raw); err != nil {
		t.Fatal("native OS helper success was lost", err)
	}
	data, err := os.ReadFile(filepath.Join(root, "native-browser-hook.json"))
	if err != nil {
		t.Fatal(err)
	}
	var result struct {
		Args        []string `json:"args"`
		Environment []string `json:"environment"`
	}
	if err := json.Unmarshal(data, &result); err != nil || len(result.Args) != 1 || result.Args[0] != raw || strings.Contains(strings.Join(result.Environment, "\n"), "private-") {
		t.Fatal("platform hook changed its target or inherited backend credentials")
	}
	t.Setenv("TERMUX__USER_ID", "77")
	if err := openTermuxBrowser(context.Background(), raw); err == nil {
		t.Fatal("failed native OS helper reported success")
	}
	t.Setenv("TERMUX__USER_ID", "88")
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	started := time.Now()
	if err := OpenBrowser(ctx, raw); !errors.Is(err, context.DeadlineExceeded) || time.Since(started) > time.Second {
		t.Fatal("cancelled native UI hook did not settle without starting a fallback", err)
	}
}
