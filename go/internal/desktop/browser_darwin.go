//go:build darwin && !ios

package desktop

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/ebitengine/purego/objc"
)

func macBrowserEnvironment(raw string) []string {
	var result []string
	for _, key := range []string{"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"} {
		if value, ok := os.LookupEnv(key); ok {
			result = append(result, key+"="+value)
		}
	}
	return append(result, "DEEPSEEK_DESKTOP_KIND=browser", "DEEPSEEK_BROWSER_URL="+raw)
}

func openPlatformBrowser(raw string) error {
	if err := validateBrowserURL(raw); err != nil {
		return err
	}
	executable, err := os.Executable()
	if err != nil {
		return errors.New("default browser helper unavailable")
	}
	helper := filepath.Join(filepath.Dir(executable), "deepseek-desktop")
	stat, err := os.Lstat(helper)
	if err != nil || !stat.Mode().IsRegular() || stat.Mode().Perm()&0o111 == 0 {
		return errors.New("default browser helper unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	// NSWorkspace executes on the helper's initial thread. A single sanitized
	// environment value carries the URL; process arguments contain no token.
	command := exec.CommandContext(ctx, helper)
	command.Env = macBrowserEnvironment(raw)
	command.WaitDelay = time.Second
	if err := command.Run(); err != nil {
		return errors.New("default browser handler unavailable")
	}
	return nil
}

// OpenMacBrowserOnInitialThread is used only by deepseek-desktop's browser mode.
// The UI process has no writer/worker/provider authority and starts no backend.
func OpenMacBrowserOnInitialThread(raw string) error {
	if err := validateBrowserURL(raw); err != nil {
		return err
	}
	if err := loadMacRuntime(); err != nil {
		return err
	}
	pool := macClass("NSAutoreleasePool").Send(macSEL("new"))
	defer pool.Send(macSEL("drain"))
	url := macClass("NSURL").Send(macSEL("URLWithString:"), macString(raw))
	workspace := macClass("NSWorkspace").Send(macSEL("sharedWorkspace"))
	if url == 0 || !objc.Send[bool](workspace, macSEL("openURL:"), url) {
		return errors.New("default browser handler unavailable")
	}
	return nil
}
