//go:build integration

package worker

import (
	"context"
	"os"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
)

func TestRustWorkerWithoutAuthorityFailsClosedAndKeepsMissingEffectUnknown(t *testing.T) {
	target := os.Getenv("DEEPSEEK_TEST_RUST_WORKER_TARGET")
	if target == "" {
		t.Fatal("DEEPSEEK_TEST_RUST_WORKER_TARGET is required for integration tests")
	}
	client, err := DialPlaintextLoopback(target)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err = client.Admit(
		ctx,
		actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP,
		&commonv1.ActionFence{ActionId: "integration-act-1", ExecutionEpoch: 1},
	)
	if err != internalprotocol.ErrFenceMismatch {
		t.Fatalf("uninitialized Rust authority must fail closed: %v", err)
	}
	state, err := client.QueryEffect(
		ctx,
		&commonv1.ActionFence{ActionId: "integration-act-1", ExecutionEpoch: 1},
	)
	if state != commonv1.EffectState_EFFECT_STATE_UNKNOWN || err != internalprotocol.ErrUnknownEffect {
		t.Fatalf("missing Rust effect must remain unknown: %v %v", state, err)
	}
}
