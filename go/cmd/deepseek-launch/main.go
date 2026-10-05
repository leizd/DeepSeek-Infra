// Command deepseek-launch is the production supervisor. It starts the Go
// control process, the Rust worker, and the Rust public listener. It does not
// start a Python interpreter, a Python service, or the stateless-mcp Node server.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(args []string) error {
	binDir := os.Getenv("DEEPSEEK_NATIVE_BIN")
	if binDir == "" {
		executable, err := os.Executable()
		if err != nil {
			return err
		}
		binDir = filepath.Dir(executable)
	}
	dataRoot := os.Getenv("DEEPSEEK_INFRA_ROOT")
	if dataRoot == "" {
		dataRoot = filepath.Join(binDir, "data")
	}
	staticDir := os.Getenv("DEEPSEEK_INFRA_STATIC_DIR")
	if staticDir == "" {
		staticDir = filepath.Join(binDir, "static")
	}
	plan, err := launch.ProductionPlan(binDir, dataRoot, staticDir)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Join(dataRoot, "go-control"), 0o755); err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	fmt.Printf("deepseek-launch gateway %s\n", plan.GatewayURL)
	if !headless(args) {
		fmt.Printf("open %s\n", plan.GatewayURL)
	}
	return launch.Run(ctx, plan.Processes, plan.Env, nil)
}

func headless(args []string) bool {
	for _, arg := range args {
		if arg == "--server" {
			return true
		}
	}
	return false
}
