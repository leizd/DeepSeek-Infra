//go:build integration

package worker

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/action"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
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

func TestRustWorkerStorageMutationFailsClosedWithoutAuth(t *testing.T) {
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

	fence := &commonv1.ActionFence{ActionId: "integration-storage-1", ExecutionEpoch: 1}
	req := &actionv1.StorageMutationRequest{
		Fence:       fence,
		OperationId: "op-storage-1",
	}

	_, err = client.ExecuteStorageMutation(ctx, req, "")
	if !errors.Is(err, internalprotocol.ErrServiceAuthenticationUnavailable) {
		t.Fatalf("unauthenticated ExecuteStorageMutation must fail closed: %v", err)
	}

	_, err = client.QueryStorageEffect(ctx, fence, "op-storage-1", "")
	if !errors.Is(err, internalprotocol.ErrServiceAuthenticationUnavailable) {
		t.Fatalf("unauthenticated QueryStorageEffect must fail closed: %v", err)
	}
}

func TestRustWorkerStorageMutationWithBearerToken(t *testing.T) {
	token := os.Getenv("DEEPSEEK_TEST_RUST_WORKER_BEARER_TOKEN")
	if token == "" {
		t.Skip("DEEPSEEK_TEST_RUST_WORKER_BEARER_TOKEN required for authenticated storage mutation test")
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

	fence := &commonv1.ActionFence{ActionId: "integration-storage-auth-1", ExecutionEpoch: 1}
	req := &actionv1.StorageMutationRequest{
		Fence:       fence,
		OperationId: "op-storage-auth-1",
	}
	_, err = client.ExecuteStorageMutation(ctx, req, "invalid-token-value")
	if !errors.Is(err, ErrWorkerPlaintextCredential) {
		t.Fatalf("plaintext must not send a bearer credential: %v", err)
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

func TestRustCoordinatorStorageActionAgainstRealWorker(t *testing.T) {
	target := os.Getenv("DEEPSEEK_TEST_RUST_WORKER_TARGET")
	if target == "" {
		t.Fatal("DEEPSEEK_TEST_RUST_WORKER_TARGET is required for integration tests")
	}
	client, err := DialPlaintextLoopback(target)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	tempDir := t.TempDir()
	controlStore, err := store.OpenControl(store.OpenOptions{
		Path:         tempDir,
		Owner:        "coord-test-owner",
		LeaseSeconds: 60,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer controlStore.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	coord := action.NewCoordinator(controlStore, client)
	fence := &commonv1.ActionFence{ActionId: "coord-act-1", ExecutionEpoch: 1}
	emptyDigest := sha256.Sum256(nil)
	req := &actionv1.StorageMutationRequest{
		Fence:        fence,
		OperationId:  "coord-op-1",
		MutationType: "PUT_CHUNK", Provider: "s3", TargetIdentity: strings.Repeat("a", 64),
		Bucket: "qualification", ObjectKey: "object", PayloadDigest: hex.EncodeToString(emptyDigest[:]),
		Precondition: &actionv1.StoragePrecondition{ConditionType: actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_CREATE_ONLY},
	}

	// 1. Action does not exist in store -> ErrActionNotFound
	_, err = coord.ExecuteStorageAction(ctx, "coord-act-1", req)
	if !errors.Is(err, action.ErrActionNotFound) {
		t.Fatalf("expected ErrActionNotFound, got %v", err)
	}

	// 2. Insert action into store in PENDING state
	act := store.Record{
		Domain:         "action",
		ID:             "coord-act-1",
		State:          "PENDING",
		Revision:       1,
		ExecutionEpoch: 1,
		Payload:        json.RawMessage(`{"command":"execute-storage-put","actionId":"coord-act-1","executionEpoch":1,"fencingToken":4}`),
	}
	if err := controlStore.Put(act); err != nil {
		t.Fatal(err)
	}

	// 3. Unauthenticated worker -> fails closed with ErrServiceAuthenticationUnavailable
	// Because this is a definite pre-effect failure, action transitions to FAILED_BEFORE_EFFECT.
	_, err = coord.ExecuteStorageAction(ctx, "coord-act-1", req)
	if !errors.Is(err, internalprotocol.ErrServiceAuthenticationUnavailable) {
		t.Fatalf("expected ErrServiceAuthenticationUnavailable, got %v", err)
	}

	rec, exists, err := controlStore.Get("action", "coord-act-1")
	if err != nil || !exists {
		t.Fatalf("action lookup failed: exists=%v, err=%v", exists, err)
	}
	if rec.State != "FAILED_BEFORE_EFFECT" {
		t.Fatalf("expected state FAILED_BEFORE_EFFECT, got %s", rec.State)
	}

	// 4. Lose an actual Rust rejection ACK, reopen Go, then query under the
	// persisted operation identity. No provider write or effect is fabricated.
	unknownAct := store.Record{
		Domain:         "action",
		ID:             "coord-act-unknown",
		State:          "PENDING",
		Revision:       1,
		ExecutionEpoch: 1,
		Payload:        json.RawMessage(`{"command":"execute-storage-put","actionId":"coord-act-unknown","executionEpoch":1,"fencingToken":4}`),
	}
	if err := controlStore.Put(unknownAct); err != nil {
		t.Fatal(err)
	}
	unknownReq := proto.Clone(req).(*actionv1.StorageMutationRequest)
	unknownReq.Fence = &commonv1.ActionFence{ActionId: unknownAct.ID, ExecutionEpoch: 1}
	unknownReq.OperationId = "coord-op-unknown"
	dropped := &discardStorageACK{Client: client}
	_, err = action.NewCoordinator(controlStore, dropped).ExecuteStorageAction(ctx, unknownAct.ID, unknownReq)
	if !errors.Is(err, action.ErrStorageMutationUncertain) || !errors.Is(dropped.receivedErr, internalprotocol.ErrServiceAuthenticationUnavailable) {
		t.Fatalf("must drop a real Rust authentication rejection: coordinator=%v received=%v", err, dropped.receivedErr)
	}
	if err := controlStore.Close(); err != nil {
		t.Fatal(err)
	}
	recovered, err := store.OpenControl(store.OpenOptions{Path: tempDir, Owner: "coord-successor"})
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.Close()
	coord = action.NewCoordinator(recovered, client)
	_, reconErr := coord.ReconcileStorageAction(ctx, "coord-act-unknown", "")
	if !errors.Is(reconErr, internalprotocol.ErrServiceAuthenticationUnavailable) {
		t.Fatalf("expected ErrServiceAuthenticationUnavailable, got %v", reconErr)
	}
	recUnknown, exists, err := recovered.Get("action", "coord-act-unknown")
	if err != nil || !exists {
		t.Fatalf("action lookup failed: exists=%v, err=%v", exists, err)
	}
	if recUnknown.State != "EFFECT_UNKNOWN" {
		t.Fatalf("expected state EFFECT_UNKNOWN, got %s", recUnknown.State)
	}
}

type discardStorageACK struct {
	*Client
	receivedErr error
}

// Exercise the explicit leased coordinator over an actual Rust RPC connection.
// An unconfigured worker denies mutation; this is not a provider-write proof.
func TestRustWorkerLeasedCoordinatorRetainsUnknownAfterAuthRejection(t *testing.T) {
	target := os.Getenv("DEEPSEEK_TEST_RUST_WORKER_TARGET")
	if target == "" {
		t.Fatal("DEEPSEEK_TEST_RUST_WORKER_TARGET is required")
	}
	client, err := DialPlaintextLoopback(target)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "leased-rpc-integration"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	id := "leased-rust-auth-rejection"
	if err := control.Put(store.Record{Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: id, LeaseSeconds: 30, ResourceKeys: []string{"rust-rpc-target"}})
	if err != nil {
		t.Fatal(err)
	}
	bytes := []byte("no provider dispatch is authorized")
	digest := sha256.Sum256(bytes)
	req := &actionv1.StorageMutationRequest{OperationId: "leased-rpc-operation", RequestId: "leased-rpc-request", Nonce: "leased-rpc-nonce",
		MutationType: "PUT_CHUNK", Provider: "s3", TargetIdentity: strings.Repeat("a", 64), Bucket: "qualification", ObjectKey: "denied",
		Payload: bytes, PayloadDigest: hex.EncodeToString(digest[:]), ExpectedLength: uint64(len(bytes)),
		Precondition: &actionv1.StoragePrecondition{ConditionType: actionv1.StorageConditionType_STORAGE_CONDITION_TYPE_CREATE_ONLY}}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err = action.NewCoordinator(control, client).ExecuteClaimedStorageAction(ctx, claim.Lease, req)
	if !errors.Is(err, action.ErrStorageMutationUncertain) || !errors.Is(err, internalprotocol.ErrServiceAuthenticationUnavailable) {
		t.Fatalf("Rust rejection was not retained as uncertainty: %v", err)
	}
	record, _, err := control.Get("action", id)
	if err != nil || record.State != "EFFECT_UNKNOWN" {
		t.Fatalf("state=%s err=%v", record.State, err)
	}
	resources, err := control.GetResourceLeases(id)
	if err != nil || len(resources) != 1 {
		t.Fatalf("Rust rejection released reservation: %v", err)
	}
	dispatch, bound, err := control.GetStorageDispatch(id, claim.Lease.Epoch)
	if err != nil || !bound || dispatch.Intent.OperationID != req.OperationId {
		t.Fatalf("lost operation identity: %v", err)
	}
}

func (c *discardStorageACK) ExecuteStorageMutation(ctx context.Context, request *actionv1.StorageMutationRequest, bearer string) (*actionv1.StorageMutationResponse, error) {
	_, c.receivedErr = c.Client.ExecuteStorageMutation(ctx, request, bearer)
	return nil, context.DeadlineExceeded
}
