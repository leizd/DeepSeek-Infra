package launch

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/desktop/entry"
)

// WithDesktop supervises the platform window alongside the production children.
// A normal window close stops the backend; a failed window remains a failure.
// No command-line argument or log contains the private launch credential.
func WithDesktop(plan Plan, binDir, dataRoot string) (Plan, error) {
	return withDesktop(plan, binDir, dataRoot, false)
}

// WithLauncherDesktop hosts only the private Go launcher page. It receives the
// ephemeral UI credential and no provider/control credentials or writer handles.
func WithLauncherDesktop(plan Plan, binDir, dataRoot string) (Plan, error) {
	return withDesktop(plan, binDir, dataRoot, true)
}

func withDesktop(plan Plan, binDir, dataRoot string, launcher bool) (Plan, error) {
	path, err := resolveBinary(binDir, "deepseek-desktop")
	if err != nil {
		return Plan{}, err
	}
	root, err := filepath.Abs(dataRoot)
	if err != nil {
		return Plan{}, err
	}
	var token string
	env := make([]string, 0, 8)
	for _, value := range plan.Env {
		key, val, _ := strings.Cut(value, "=")
		switch key {
		case "AUTH_TOKEN":
			token = val
		case "SYSTEMROOT", "WINDIR", "TEMP", "TMP":
			env = append(env, value)
		default:
			for _, rootKey := range platformRootKeys {
				if key == rootKey {
					env = append(env, value)
					break
				}
			}
		}
	}
	privateURL, err := entry.EntryURL(plan.GatewayURL, token)
	if err != nil {
		return Plan{}, err
	}
	// Session addresses belong only to the system UI. Provider, interpreter and
	// library-loader configuration stays out of the platform process.
	for _, key := range desktopSessionKeys {
		if value, exists := os.LookupEnv(key); exists {
			env = append(env, key+"="+value)
		}
	}
	profile := ".desktop-webview"
	if launcher {
		profile = ".launcher-webview"
		env = append(env, "DEEPSEEK_DESKTOP_KIND=launcher")
	}
	env = append(env, "DEEPSEEK_DESKTOP_URL="+privateURL, "DEEPSEEK_DESKTOP_PROFILE="+filepath.Join(root, profile))
	plan.Processes = append(append([]Process(nil), plan.Processes...), Process{Name: "deepseek-desktop", Path: path, Env: env})
	return plan, nil
}

var desktopSessionKeys = []string{
	"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
	"DBUS_SESSION_BUS_ADDRESS", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
}
