package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"

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
	addr, err := lifecycle.Listen(ctx, cfg)
	if err != nil {
		return err
	}
	payload, err := json.Marshal(lifecycle.StatusFrom(cfg))
	if err != nil {
		return err
	}
	fmt.Printf("deepseekd listening on %s %s\n", addr, payload)
	<-ctx.Done()
	return nil
}
