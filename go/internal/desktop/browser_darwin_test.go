//go:build darwin && !ios

package desktop

import (
	"strings"
	"testing"
)

func TestNativeMacBrowserHelperExcludesBackendAndLoaderCredentials(t *testing.T) {
	t.Setenv("AUTH_TOKEN", "private-backend")
	t.Setenv("DEEPSEEK_CONTROL_SIGNER_PRIVATE_KEY", "private-control")
	t.Setenv("DEEPSEEK_API_KEY", "private-provider")
	t.Setenv("DYLD_INSERT_LIBRARIES", "private-loader")
	t.Setenv("DEEPSEEK_DESKTOP_PROFILE", "private-profile")
	t.Setenv("HOME", "/controlled-native-home")
	const target = "http://127.0.0.1:8123/?token=fixture-public-launch"
	env := macBrowserEnvironment(target)
	joined := strings.Join(env, "\n")
	if strings.Contains(joined, "private-") || !strings.Contains(joined, "HOME=/controlled-native-home") || !strings.Contains(joined, "DEEPSEEK_BROWSER_URL="+target) {
		t.Fatal("browser helper environment differs")
	}
	for _, invalid := range []string{"file:///private", "https://user:secret@127.0.0.1/", "https://example.com/", "http://127.0.0.1/\nsecret"} {
		if err := OpenMacBrowserOnInitialThread(invalid); err == nil {
			t.Fatal("invalid browser URL reached Cocoa")
		}
	}
}
