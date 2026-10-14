//go:build !windows && (!linux || android || deepseek_android)

package desktop

import (
	"context"
	"strings"
	"testing"
)

func TestMissingPlatformBrowserLeavesManualURLFlowWithoutReportingSuccess(t *testing.T) {
	t.Setenv("TERMUX_VERSION", "")
	const target = "http://127.0.0.1:8123/?token=private-launch-fixture"
	if err := OpenBrowser(context.Background(), target); err == nil || !strings.Contains(err.Error(), "unavailable") || strings.Contains(err.Error(), "private-launch-fixture") {
		t.Fatal("missing platform browser reported success or leaked the launch credential", err)
	}
	t.Setenv("TERMUX_VERSION", "0.118")
	t.Setenv("PATH", t.TempDir())
	if err := OpenBrowser(context.Background(), target); err == nil || strings.Contains(err.Error(), "private-launch-fixture") {
		t.Fatal("missing Termux helper reported an OS browser success or leaked credentials", err)
	}
}
