//go:build integration

package worker

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"os"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
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

func TestRustWorkerUnconfiguredInstallRemainsFailClosed(t *testing.T) {
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
	canonical, fence := frozenAuthorityRequest(t)
	if err := client.InstallAuthoritativeEpoch(ctx, fence, canonical); err != store.ErrAuthorityRequestSignerMismatch {
		t.Fatalf("unconfigured worker must not install a live epoch: %v", err)
	}
	if err := client.Admit(ctx, actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, fence); err != internalprotocol.ErrFenceMismatch {
		t.Fatalf("unsigned epoch must remain uninstalled: %v", err)
	}
}

func TestRustWorkerInstallsEpochFromSignedAuthorityRequest(t *testing.T) {
	if os.Getenv("DEEPSEEK_TEST_RUST_WORKER_AUTHORITY") != "1" {
		t.Skip("set DEEPSEEK_TEST_RUST_WORKER_AUTHORITY=1 against a configured worker")
	}
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
	canonical, fence := signLiveAuthorityRequest(t, "integration-act-auth-1", 1)
	if err := client.InstallAuthoritativeEpoch(ctx, fence, canonical); err != nil {
		t.Fatalf("signed install: %v", err)
	}
	if err := client.Admit(ctx, actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, fence); err != nil {
		t.Fatalf("admitted after signed install: %v", err)
	}
	if err := client.InstallAuthoritativeEpoch(ctx, fence, canonical); err != internalprotocol.ErrStaleEpoch {
		t.Fatalf("retry of an installed epoch: %v", err)
	}
	stale, staleFence := signLiveAuthorityRequest(t, "integration-act-auth-1", 1)
	if err := client.InstallAuthoritativeEpoch(ctx, staleFence, stale); err != internalprotocol.ErrStaleEpoch {
		t.Fatalf("stale epoch: %v", err)
	}
}

func signLiveAuthorityRequest(t *testing.T, actionID string, epoch int) ([]byte, *commonv1.ActionFence) {
	t.Helper()
	seed, err := hex.DecodeString("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
	if err != nil {
		t.Fatal(err)
	}
	private := ed25519.NewKeyFromSeed(seed)
	public := private.Public().(ed25519.PublicKey)
	issuedAt := time.Now().UTC().Truncate(time.Second)
	requestID := randomHex64(t)
	nonce := randomHex64(t)
	unsigned := map[string]any{
		"schema":         store.AuthorityRequestSchema,
		"schemaVersion":  1,
		"domain":         "action",
		"operation":      "install-epoch",
		"actionId":       actionID,
		"executionEpoch": epoch,
		"fencingToken":   4,
		"revision":       1,
		"requestId":      requestID,
		"nonce":          nonce,
		"issuedAt":       issuedAt.Format("2006-01-02T15:04:05Z"),
		"expiresAt":      issuedAt.Add(5 * time.Minute).Format("2006-01-02T15:04:05Z"),
		"runtime":        store.RuntimeGo,
		"mode":           store.ModeShadow,
		"fleetId":        "fleet-a",
		"environment":    "test",
		"role":           "control-plane",
		"payload":        map[string]any{},
	}
	_, raw, err := store.SignAuthorityRequest(unsigned, private, base64.RawURLEncoding.EncodeToString(public))
	if err != nil {
		t.Fatal(err)
	}
	fence, err := store.FenceFromAuthorityRequest(raw)
	if err != nil {
		t.Fatal(err)
	}
	return raw, fence
}

func randomHex64(t *testing.T) string {
	t.Helper()
	var raw [32]byte
	if _, err := rand.Read(raw[:]); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(raw[:])
}
