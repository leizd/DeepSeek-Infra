//go:build linux && !android && !deepseek_android

package launch

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestLinuxCancelledLaunchAndInvalidGuardianPlanAdmitNoChild(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := Run(ctx, []Process{{Name: "deepseekd", Path: os.Args[0]}}, nil, io.Discard); !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled launch admitted a child or lost cancellation", err)
	}
	if err := Run(context.Background(), []Process{{Name: "foreign", Path: os.Args[0]}}, nil, io.Discard); err == nil || !strings.Contains(err.Error(), "invalid native guardian process") {
		t.Fatal("a foreign plan name bypassed the guardian", err)
	}
}

func TestLinuxRemovedWorkingDirectoryCannotCreateRelativeRuntimeState(t *testing.T) {
	bin := t.TempDir()
	makeNativePlanBinaries(t, bin)
	if err := os.WriteFile(filepath.Join(bin, "deepseek-desktop"), []byte("selection fixture"), 0o755); err != nil {
		t.Fatal(err)
	}
	removed := t.TempDir()
	t.Chdir(removed)
	if err := os.Remove(removed); err != nil {
		t.Fatal(err)
	}
	for _, roots := range [][2]string{{"relative-data", bin}, {bin, "relative-static"}} {
		plan, err := ProductionPlan(bin, roots[0], roots[1])
		if err == nil || len(plan.Processes) != 0 {
			t.Fatal("removed working directory admitted unresolved runtime state")
		}
	}
	if _, err := WithDesktop(Plan{GatewayURL: "http://127.0.0.1:8000/"}, bin, "relative-profile"); err == nil {
		t.Fatal("removed working directory admitted a relative browser profile")
	}
}

func TestLinuxUnavailableDescriptorBudgetFailsBeforeStartingGuardian(t *testing.T) {
	fence, err := newProcessFence()
	if err != nil {
		t.Fatal(err)
	}
	defer fence.close()
	var original unix.Rlimit
	if err := unix.Getrlimit(unix.RLIMIT_NOFILE, &original); err != nil {
		t.Fatal(err)
	}
	limited := original
	limited.Cur = 0
	if err := unix.Setrlimit(unix.RLIMIT_NOFILE, &limited); err != nil {
		t.Fatal(err)
	}
	defer unix.Setrlimit(unix.RLIMIT_NOFILE, &original)
	_, prepareError := fence.prepare(Process{Name: "deepseekd", Path: os.Args[0]}, nil)
	restoreError := unix.Setrlimit(unix.RLIMIT_NOFILE, &original)
	if restoreError != nil {
		t.Fatal(restoreError)
	}
	if !errors.Is(prepareError, unix.EMFILE) || len(*fence.pipes) != 0 {
		t.Fatal("descriptor exhaustion admitted an unmonitored child", prepareError)
	}
}

func TestLinuxLegacyCommandAfterNativeAdmissionSettlesOwnedChildren(t *testing.T) {
	ready := t.TempDir()
	processes := []Process{{Name: "deepseekd", Path: os.Args[0]}, {Name: "deepseek-worker", Path: "/usr/bin/python3"}}
	env := append(os.Environ(), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_LAUNCH_READY_DIR="+ready)
	if err := Run(context.Background(), processes, env, io.Discard); err == nil || !strings.Contains(err.Error(), "refusing legacy runtime") {
		t.Fatal("later legacy command bypassed admission", err)
	}
	entries, err := os.ReadDir(ready)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		listeners := readLaunchListeners(t, ready, len(entries))
		for _, address := range listeners {
			connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
			if err == nil {
				connection.Close()
				t.Fatal("invalid later process left the first native listener running")
			}
		}
	}
}

func TestLinuxWaitForChildrenHasBoundedMissingResult(t *testing.T) {
	started := time.Now()
	waitCount(make(chan processExit), 1)
	if elapsed := time.Since(started); elapsed < 4*time.Second || elapsed > 7*time.Second {
		t.Fatal("missing child result lost its shutdown bound", elapsed)
	}
}
