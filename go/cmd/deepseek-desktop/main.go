// Command deepseek-desktop is platform UI glue for the Rust public listener.
// It holds no business writer, worker authority, provider key or tool bridge.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"runtime"
	"syscall"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/desktop"
	"github.com/leizd/DeepSeek-Infra/go/internal/desktop/entry"
)

// Platform UI frameworks require creation and message handling on the initial
// thread. Only this UI process pins it; headless supervision stays independent.
func init() { runtime.LockOSThread() }

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	privateURL, err := entry.EntryURL(os.Getenv("DEEPSEEK_DESKTOP_URL"), "")
	if err != nil {
		return err
	}
	// A killed control writer retains its 30-second durable lease. Give the
	// legitimate startup claim time to wait for expiry, without a forced takeover.
	ready, stop := context.WithTimeout(ctx, 40*time.Second)
	launcher := os.Getenv("DEEPSEEK_DESKTOP_KIND") == "launcher"
	if launcher {
		err = entry.WaitLauncherReady(ready, privateURL)
	} else {
		err = entry.WaitControlReady(ready, privateURL)
	}
	stop()
	if err != nil {
		return err
	}
	if launcher {
		return desktop.ShowLauncher(ctx, privateURL, os.Getenv("DEEPSEEK_DESKTOP_PROFILE"))
	}
	return desktop.Show(ctx, privateURL, os.Getenv("DEEPSEEK_DESKTOP_PROFILE"))
}
