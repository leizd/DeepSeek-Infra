// Command deepseek-launch is the production supervisor. It starts the Go
// control process, the Rust worker, and the Rust public listener. It does not
// start a Python interpreter, a Python service, or the stateless-mcp Node server.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
	"github.com/leizd/DeepSeek-Infra/go/internal/launchgui"
	"golang.org/x/term"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(args []string) error {
	options, err := launch.ParseOptions(args, os.Getenv)
	if errors.Is(err, flag.ErrHelp) {
		fmt.Println("deepseek-launch [--app|--gui|--server|--mobile] [--host IP] [--port PORT] [--lan] [--api-key KEY] [--tavily-api-key KEY] [--auth-disabled] [--ocr] [--no-open] [--no-prompt]")
		return nil
	}
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	options, err = options.PromptAPIKey(term.IsTerminal(int(os.Stdin.Fd())), func() ([]byte, error) {
		value, err := readConsoleSecret(ctx)
		if errors.Is(err, context.Canceled) {
			stop()
		}
		return value, err
	})
	if err != nil {
		if ctx.Err() != nil {
			return nil
		}
		return err
	}
	if ctx.Err() != nil {
		return nil
	}
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
	if options.Mode == "gui" {
		initial := launcherconfig.Config{DeepSeekAPIKey: options.APIKey, TavilyAPIKey: options.TavilyAPIKey, Host: options.Host, Port: options.Port, AllowLAN: options.Host == "0.0.0.0", OCREnabled: options.OCR, AuthDisabled: options.AuthDisabled}
		return launchgui.Run(ctx, launchgui.Native{BinDir: binDir, DataRoot: dataRoot, StaticDir: staticDir}, initial)
	}
	plan, err := launch.ProductionPlanWithAddress(binDir, dataRoot, staticDir, options.BindAddress())
	if err != nil {
		return err
	}
	plan, err = launch.WithLocalAuth(options.Apply(plan), dataRoot)
	if err != nil {
		return err
	}
	if options.Mode == "app" {
		plan, err = launch.WithDesktop(plan, binDir, dataRoot)
		if err != nil {
			return err
		}
	}
	if err := os.MkdirAll(filepath.Join(dataRoot, "go-control"), 0o755); err != nil {
		return err
	}
	fmt.Printf("deepseek-launch gateway %s\n", plan.GatewayURL)
	if options.Mode == "mobile" {
		return runMobile(ctx, plan, options)
	}
	return launch.Run(ctx, plan.Processes, plan.Env, nil)
}
