package lifecycle

import (
	"context"
	"errors"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

const DefaultStartupWait = 35 * time.Second

// StartWithWriterWait waits only for an existing writer lease to release or
// expire. Every retry uses the normal durable claim; no fence or epoch is reset.
// The retry deadline does not shorten the successfully started server's life.
func StartWithWriterWait(ctx context.Context, cfg config.Config, limit time.Duration) (*Server, error) {
	wait, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	var last error
	for {
		if err := wait.Err(); err != nil {
			return nil, errors.Join(last, err)
		}
		server, err := Start(ctx, cfg)
		if !errors.Is(err, store.ErrWriterFenceHeld) {
			return server, err
		}
		last = err
		timer := time.NewTimer(200 * time.Millisecond)
		select {
		case <-wait.Done():
			timer.Stop()
			return nil, errors.Join(last, wait.Err())
		case <-timer.C:
		}
	}
}
