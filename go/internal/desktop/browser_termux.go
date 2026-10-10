package desktop

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"time"
)

// This is Android UI glue: the official helper issues ACTION_VIEW through am.
// It receives one validated URL and no backend credentials or arbitrary command.
func openTermuxBrowser(ctx context.Context, raw string) error {
	if err := validateBrowserURL(raw); err != nil {
		return err
	}
	path, err := exec.LookPath("termux-open-url")
	if err != nil {
		return errors.New("mobile browser handler unavailable")
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, path, raw)
	cmd.Env = termuxBrowserEnvironment()
	if err := cmd.Run(); err != nil {
		return errors.New("mobile browser open failed")
	}
	return nil
}

func termuxBrowserEnvironment() []string {
	var env []string
	for _, key := range []string{"PATH", "HOME", "PREFIX", "TMPDIR", "LANG", "LC_ALL", "TERMUX_VERSION", "TERMUX__USER_ID", "ANDROID_ROOT", "ANDROID_DATA", "ANDROID_ART_ROOT", "ANDROID_I18N_ROOT", "ANDROID_TZDATA_ROOT", "BOOTCLASSPATH", "DEX2OATBOOTCLASSPATH", "SYSTEMSERVERCLASSPATH"} {
		if value, exists := os.LookupEnv(key); exists {
			env = append(env, key+"="+value)
		}
	}
	return env
}
