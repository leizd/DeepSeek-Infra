package api

import (
	"context"
	"log"
	"os"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

type mediaRecoveryWorker interface {
	mediaPatchQueryWorker
	Close() error
}

type mediaRecoveryDial func() (mediaRecoveryWorker, error)

func dialMediaRecoveryWorker() (mediaRecoveryWorker, error) { return dialMediaWorker() }

// StartMediaPatchRecovery belongs to the shared authoritative control lifecycle.
// It only queries original operations; it cannot execute or admit a replacement.
func StartMediaPatchRecovery(ctx context.Context, control *store.Control, health ControlPlaneHealth) <-chan struct{} {
	done := make(chan struct{})
	if control == nil || health.Mode != controlv1.RuntimeMode_RUNTIME_MODE_AUTHORITATIVE || health.MutationAuthority != config.MutationAuthorityGo || os.Getenv("DEEPSEEK_RUNTIME_MODE") != "python_disabled" {
		close(done)
		return done
	}
	go func() {
		defer close(done)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		runMediaPatchRecovery(ctx, control, ticker.C, dialMediaRecoveryWorker)
	}()
	return done
}

func runMediaPatchRecovery(ctx context.Context, control *store.Control, ticks <-chan time.Time, dial mediaRecoveryDial) {
	for {
		if ctx.Err() != nil {
			return
		}
		reconcilePendingMediaPatch(ctx, control, time.Now().Unix(), dial)
		select {
		case <-ctx.Done():
			return
		case <-ticks:
		}
	}
}

func reconcilePendingMediaPatch(ctx context.Context, control *store.Control, now int64, dial mediaRecoveryDial) {
	if ctx.Err() != nil {
		return
	}
	pending, err := control.PendingMediaPatch()
	if err != nil || pending == nil {
		return // Includes loss of the current transactional writer fence.
	}
	writer := control.Writer().FencingToken
	// Do not interrupt a request still executing under this live writer/lease.
	// A successor can query the older operation immediately without rebinding it.
	if !(pending.WriterFencingToken < writer || pending.WriterFencingToken == writer && now >= pending.LeaseUntilUnix) {
		return
	}
	client, err := dial()
	if err != nil {
		return
	}
	defer client.Close()
	queryCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if _, settled := recoverMediaPatch(queryCtx, control, client, pending); settled {
		log.Printf("media recovery settled original operation=%s", pending.Intent.OperationId)
	}
}
