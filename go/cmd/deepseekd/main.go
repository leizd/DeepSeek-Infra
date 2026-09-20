package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"

	"github.com/leizd/DeepSeek-Infra/go/internal/a2a"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/lifecycle"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	runtime, err := lifecycle.Start(ctx, cfg)
	if err != nil {
		return err
	}
	taskControl, err := a2a.StartConfigured(ctx)
	if err != nil {
		stop()
		<-runtime.Done()
		return err
	}
	if taskControl != nil {
		defer taskControl.Close()
		fmt.Printf("deepseekd A2A control listening on %s (mTLS, isolated qualification store)\n", taskControl.Addr())
	}
	payload, err := json.Marshal(lifecycle.StatusFrom(cfg, cfg.ShadowStoreDir != ""))
	if err != nil {
		return err
	}
	fmt.Printf("deepseekd listening on %s %s\n", runtime.Addr(), payload)
	if taskControl != nil {
		select {
		case err := <-runtime.Done():
			return err
		case err := <-taskControl.Done():
			wasCanceled := ctx.Err() != nil
			stop()
			<-runtime.Done()
			if wasCanceled {
				return err
			}
			return fmt.Errorf("A2A control listener stopped: %v", err)
		}
	}
	return <-runtime.Done()
}
