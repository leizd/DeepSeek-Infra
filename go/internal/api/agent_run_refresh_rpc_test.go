package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fixtureAgentRefresher struct {
	authorizer agentExecutionAuthorizer
	refresh    fixtureAgentAuthorizer
}

func (fixture fixtureAgentRefresher) AuthorizeAgentExecution(ctx context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
	return fixture.authorizer.AuthorizeAgentExecution(ctx, claim, id, nonce, binding)
}

func (fixture fixtureAgentRefresher) RefreshAgentExecution(ctx context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
	return fixture.refresh(ctx, claim, id, nonce, binding)
}

func agentRefreshInput(claim *agentv1.AgentRunExecutionClaim) *agentv1.RefreshAgentRunAuthorizationRequest {
	return &agentv1.RefreshAgentRunAuthorizationRequest{SchemaVersion: 1, OperationId: "phase-grant-refresh-1",
		Fence: proto.Clone(claim.Fence).(*commonv1.ActionFence), RunId: claim.RunId, Owner: claim.Owner,
		ClaimToken: claim.ClaimToken, LeaseSeconds: 60}
}

func agentRefreshFixture(t *testing.T) (*store.Control, *agentRunService, *agentv1.AgentRunExecutionClaim, *agentExecutionSignerSession, ed25519.PrivateKey) {
	t.Helper()
	control, service, _, request, private := agentAuthorizeFixture(t, "tasks")
	authorized, err := service.AuthorizeExecution(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := service.executionSigner(control)
	if err != nil {
		t.Fatal(err)
	}
	return control, service, authorized.Claim, signer, private
}

func TestAgentGrantRefreshUsesLiveClaimAndOriginalReceiptForExactRetry(t *testing.T) {
	control, service, claim, base, private := agentRefreshFixture(t)
	originalIntent, _ := agentSigningIntent(claim)
	initialID, initialNonce := agentSigningIdentifiers(originalIntent)
	receipts := map[string][]byte{}
	var issuances, closes int
	service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
		return &agentExecutionSignerSession{binding: base.binding, close: func() error { closes++; return nil },
			client: fixtureAgentRefresher{authorizer: base.client, refresh: func(_ context.Context, current *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
				intent, err := agentSigningIntent(current)
				if err != nil || intent != originalIntent || current.LeaseUntil < claim.LeaseUntil || id == initialID || nonce == initialNonce {
					t.Fatal("refresh changed immutable scope or reused initial signing identity", err)
				}
				if raw, ok := receipts[id]; ok {
					return raw, nil
				}
				issuances++
				raw := fixtureAgentAuthorization(t, current, id, nonce, binding, private, nil)
				receipts[id] = raw
				return raw, nil
			}}}, nil
	}
	input := agentRefreshInput(claim)
	first, err := service.RefreshAuthorization(context.Background(), input)
	if err != nil || !first.AlreadyApplied || first.SignerPublicKey != base.binding.PublicKey || first.Claim.Fence.ExecutionEpoch != claim.Fence.ExecutionEpoch {
		t.Fatal("live refresh failed or advanced epoch", err)
	}
	retained := append([]byte(nil), first.CanonicalAuthorization...)
	first.CanonicalAuthorization[0] ^= 1 // Returned bytes cannot mutate retained signer custody.
	replay, err := service.RefreshAuthorization(context.Background(), input)
	if err != nil || !bytes.Equal(retained, replay.CanonicalAuthorization) || issuances != 1 {
		t.Fatal("exact refresh retry changed signed dates or receipt", err)
	}
	input.OperationId = "phase-grant-refresh-2"
	next, err := service.RefreshAuthorization(context.Background(), input)
	if err != nil || bytes.Equal(retained, next.CanonicalAuthorization) || issuances != 2 || closes != 3 {
		t.Fatal("new explicit operation did not receive a separate issuance", err)
	}
	after, _ := agentSigningIntent(next.Claim)
	record, found, err := control.Get("action", claim.Fence.ActionId)
	if err != nil || !found || after != originalIntent || record.ExecutionEpoch != claim.Fence.ExecutionEpoch || record.State != "CLAIMED" {
		t.Fatal("refresh claimed another epoch or completed the action", err)
	}
}

func TestAgentGrantRefreshRejectsMalformedRequestBeforeSignerOrRenewal(t *testing.T) {
	control, service, claim, _, _ := agentRefreshFixture(t)
	var calls int
	service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
		calls++
		return nil, errors.New("must not open")
	}
	changes := []func(*agentv1.RefreshAgentRunAuthorizationRequest){
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.SchemaVersion = 0 },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.OperationId = "" },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.OperationId = "../refresh" },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.Fence = nil },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.Fence.ActionId = "" },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.Fence.ExecutionEpoch = 0 },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.RunId = "" },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.Owner = "" },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.ClaimToken = "" },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.LeaseSeconds = 0 },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.LeaseSeconds = 301 },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) { i.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		func(i *agentv1.RefreshAgentRunAuthorizationRequest) {
			i.Fence.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
	}
	before, _, _ := control.Get("action", claim.Fence.ActionId)
	for _, change := range changes {
		input := agentRefreshInput(claim)
		change(input)
		if _, err := service.RefreshAuthorization(context.Background(), input); status.Code(err) != codes.InvalidArgument {
			t.Fatal("malformed refresh accepted", err)
		}
	}
	if _, err := service.RefreshAuthorization(context.Background(), nil); status.Code(err) != codes.InvalidArgument {
		t.Fatal(err)
	}
	after, _, _ := control.Get("action", claim.Fence.ActionId)
	if calls != 0 || before.Revision != after.Revision || before.ExecutionEpoch != after.ExecutionEpoch {
		t.Fatal("invalid input reached custody or changed action authority")
	}
}

func TestAgentGrantRefreshRequiresConfiguredCapableSignerBeforeLeaseMutation(t *testing.T) {
	for _, kind := range []string{"error", "nil-session", "nil-client", "no-refresh", "default-config"} {
		t.Run(kind, func(t *testing.T) {
			_, service, claim, base, _ := agentRefreshFixture(t)
			closed := false
			service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
				switch kind {
				case "error":
					return nil, errors.New("custody unavailable")
				case "nil-session":
					return nil, nil
				case "nil-client":
					return &agentExecutionSignerSession{}, nil
				default:
					return &agentExecutionSignerSession{client: base.client, binding: base.binding, close: func() error { closed = true; return nil }}, nil
				}
			}
			if kind == "default-config" {
				t.Setenv("DEEPSEEK_WORKER_TARGET", "")
				service.executionSigner = nil
			}
			if _, err := service.RefreshAuthorization(context.Background(), agentRefreshInput(claim)); status.Code(err) != codes.Unavailable {
				t.Fatal(err)
			}
			if kind == "no-refresh" && !closed {
				t.Fatal("unsupported signer session leaked")
			}
		})
	}
}

func TestAgentGrantRefreshRefusesStaleScopeAndCancellation(t *testing.T) {
	for _, kind := range []string{"run", "epoch", "owner", "token", "cancelled", "shadow", "missing-store", "effect-unknown"} {
		t.Run(kind, func(t *testing.T) {
			control, service, claim, base, _ := agentRefreshFixture(t)
			calls := 0
			service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
				return &agentExecutionSignerSession{binding: base.binding, client: fixtureAgentRefresher{authorizer: base.client,
					refresh: func(context.Context, *agentv1.AgentRunExecutionClaim, string, string, worker.ControlSignerBinding) ([]byte, error) {
						calls++
						return nil, errors.New("must not sign")
					}}}, nil
			}
			input := agentRefreshInput(claim)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			switch kind {
			case "run":
				input.RunId = "another-run"
			case "epoch":
				input.Fence.ExecutionEpoch++
			case "owner":
				input.Owner = "another-owner"
			case "token":
				input.ClaimToken += "-other"
			case "cancelled":
				cancel()
			case "shadow":
				service.health.MutationAuthority = "python"
			case "missing-store":
				service.control = nil
			case "effect-unknown":
				if _, err := control.MarkActionEffectUnknown(claim.Fence.ActionId, claim.Fence.ExecutionEpoch, claim.ClaimToken); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := service.RefreshAuthorization(ctx, input); err == nil || calls != 0 {
				t.Fatal("rebound, unavailable or cancelled scope reached signing", err, calls)
			}
		})
	}
}

func TestAgentGrantRefreshRechecksActualAuthorityAndGrantAfterIssuance(t *testing.T) {
	for _, kind := range []string{"signer-error", "cancel-after-sign", "writer-expiry", "bad-signature", "other-request", "other-nonce", "other-scope", "expired"} {
		t.Run(kind, func(t *testing.T) {
			var offset atomic.Int64
			control, service, _, request, private := agentAuthorizeFixtureWithClock(t, "tasks", func() int64 { return time.Now().Unix() + offset.Load() })
			authorized, err := service.AuthorizeExecution(context.Background(), request)
			if err != nil {
				t.Fatal(err)
			}
			base, _ := service.executionSigner(control)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
				return &agentExecutionSignerSession{binding: base.binding, client: fixtureAgentRefresher{authorizer: base.client,
					refresh: func(_ context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
						if kind == "signer-error" {
							return nil, context.DeadlineExceeded
						}
						raw := fixtureAgentAuthorization(t, claim, id, nonce, binding, private, func(doc map[string]any) {
							switch kind {
							case "other-request":
								doc["requestId"] = "another-request"
							case "other-nonce":
								doc["nonce"] = "another-nonce"
							case "other-scope":
								doc["payload"].(map[string]any)["runId"] = "another-run"
							case "expired":
								doc["expiresAt"] = time.Now().Add(-time.Second).UTC().Format(time.RFC3339)
							}
						})
						if kind == "bad-signature" {
							var doc map[string]any
							_ = json.Unmarshal(raw, &doc)
							doc["signature"] = "invalid"
							raw, _ = json.Marshal(doc)
						}
						if kind == "cancel-after-sign" {
							cancel()
						}
						if kind == "writer-expiry" {
							offset.Store(1000)
						}
						return raw, nil
					}}}, nil
			}
			if response, err := service.RefreshAuthorization(ctx, agentRefreshInput(authorized.Claim)); err == nil || response != nil {
				t.Fatal("expired/rebound/invalid issuance was returned as authorized", kind, err)
			}
		})
	}
}
