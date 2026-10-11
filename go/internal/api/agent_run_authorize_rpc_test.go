package api

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestAgentExecutionAuthorizeRequiresConfiguredRustBeforeClaim(t *testing.T) {
	for _, name := range []string{"DEEPSEEK_WORKER_TARGET", "DEEPSEEK_WORKER_TLS_CA_FILE", "DEEPSEEK_WORKER_TLS_SERVER_NAME",
		"DEEPSEEK_WORKER_SERVICE_BEARER", "DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT", "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY",
		"DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT"} {
		t.Setenv(name, "")
	}
	control := agentExecutionRPCStore(t, true)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	input := executionRPCInput(seedAgentExecutionRPC(t, client))
	ctx := controlRPCContext(t, testInternalBearer)
	before, err := control.ListAuthoritativeRecords("action")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.AuthorizeExecution(ctx, input); status.Code(err) != codes.Unavailable {
		t.Fatalf("unconfigured Rust signer should be unavailable before claiming work: %v", err)
	}
	after, err := control.ListAuthoritativeRecords("action")
	if err != nil || len(after) != len(before) {
		t.Fatal("missing signer created an execution action")
	}
}

type fixtureAgentAuthorizer func(context.Context, *agentv1.AgentRunExecutionClaim, string, string, worker.ControlSignerBinding) ([]byte, error)

func (fixture fixtureAgentAuthorizer) AuthorizeAgentExecution(ctx context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
	return fixture(ctx, claim, id, nonce, binding)
}

// Real UTC and an isolated real store. Only administrative promotion signatures
// are offline fixtures; these tests do not qualify Rust custody or effects.
func agentAuthorizeStore(t *testing.T, writerSeconds int64) *store.Control {
	t.Helper()
	return agentAuthorizeStoreWithClock(t, writerSeconds, nil)
}

func agentAuthorizeStoreWithClock(t *testing.T, writerSeconds int64, now func() int64) *store.Control {
	t.Helper()
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "agent-authorize-test", LeaseSeconds: writerSeconds, Now: now,
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionRoutePublic, FleetID: mutationFleetID, Environment: mutationEnvironment})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	checkpoint := authorityCheckpointFixture(t)
	if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
		t.Fatal(err)
	}
	for _, domain := range []string{"agent_run", "action"} {
		for _, next := range []store.CutoverState{store.CutoverDualEvaluate, store.CutoverGoAuthoritative} {
			current, err := control.GetCutover(domain)
			if err != nil {
				t.Fatal(err)
			}
			request := signedPromotionRoute(t, store.CutoverTransition{Domain: domain, To: next, ExpectedRevision: current.Revision,
				ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "authorize-" + domain + "-" + string(next), Authority: checkpoint},
				current, time.Now().UTC().Unix(), nil)
			if _, err := control.TransitionCutover(request); err != nil {
				t.Fatal(err)
			}
		}
	}
	return control
}

func agentAuthorizeFixture(t *testing.T, phase string) (*store.Control, *agentRunService, agentv1.AgentRunControlClient, *agentv1.ClaimAgentRunExecutionRequest, ed25519.PrivateKey) {
	t.Helper()
	return agentAuthorizeFixtureWithClock(t, phase, nil)
}

func agentAuthorizeFixtureWithClock(t *testing.T, phase string, now func() int64) (*store.Control, *agentRunService, agentv1.AgentRunControlClient, *agentv1.ClaimAgentRunExecutionRequest, ed25519.PrivateKey) {
	t.Helper()
	control := agentAuthorizeStoreWithClock(t, 600, now)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	epoch := uint64(0)
	if phase == "tasks" {
		epoch = seedAgentExecutionRPC(t, client)
	} else {
		authority, err := client.GetAuthority(controlRPCContext(t, testInternalBearer), &agentv1.AgentRunAuthorityInput{})
		if err != nil {
			t.Fatal(err)
		}
		epoch = authority.MetadataEpoch
		for index := int64(0); index < 2; index++ {
			if _, err := client.AppendEvent(controlRPCContext(t, testInternalBearer), agentRPCAppend(index, epoch)); err != nil {
				t.Fatal(err)
			}
		}
	}
	input := executionRPCInput(epoch)
	if phase == "plan" {
		input.Phase, input.ExpectedIndex = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, 2
	}
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{31}, ed25519.SeedSize)) // offline unit fixture only
	binding := worker.ControlSignerBinding{PublicKey: base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey)),
		FleetID: mutationFleetID, Environment: mutationEnvironment}
	service := &agentRunService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}
	service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
		return &agentExecutionSignerSession{binding: binding, client: fixtureAgentAuthorizer(func(_ context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
			return fixtureAgentAuthorization(t, claim, id, nonce, binding, private, nil), nil
		})}, nil
	}
	return control, service, client, input, private
}

func fixtureAgentAuthorization(t *testing.T, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding, private ed25519.PrivateKey, alter func(map[string]any)) []byte {
	t.Helper()
	hash := func(domain, text string) string {
		sum := sha256.Sum256([]byte(domain + "\x00" + text))
		return "sha256:" + hex.EncodeToString(sum[:])
	}
	keyID, _ := store.SignerKeyIDForPublicKey(binding.PublicKey)
	expiry := claim.LeaseUntil
	if claim.WriterLeaseUntil < expiry {
		expiry = claim.WriterLeaseUntil
	}
	document := map[string]any{"schema": store.AgentExecutionGrantSchema, "schemaVersion": 1, "domain": "action",
		"operation": store.AgentExecutionGrantOperation, "runtime": "go", "mode": "authoritative", "role": "control-plane",
		"fleetId": binding.FleetID, "environment": binding.Environment, "actionId": claim.Fence.ActionId,
		"executionEpoch": claim.Fence.ExecutionEpoch, "fencingToken": claim.WriterFencingToken, "revision": claim.ClaimRevision,
		"requestId": id, "nonce": nonce, "issuedAt": time.Now().UTC().Truncate(time.Second).Format(time.RFC3339),
		"expiresAt": time.Unix(expiry, 0).UTC().Format(time.RFC3339), "signerKeyId": keyID, "signatureAlgorithm": "Ed25519",
		"payload": map[string]any{"runId": claim.RunId, "phase": executionPhaseName(claim.Phase), "requestSha256": claim.Request.Sha256,
			"requestLength": claim.Request.Length, "planDigest": claim.PlanDigest, "metadataIndex": claim.MetadataIndex,
			"metadataEpoch": claim.MetadataEpoch, "ownerDigest": hash("agent-owner-v1", claim.Owner),
			"claimDigest": hash("agent-claim-v1", claim.ClaimToken), "claimRevision": claim.ClaimRevision,
			"writerFencingToken": claim.WriterFencingToken, "leaseUntil": claim.LeaseUntil, "writerLeaseUntil": claim.WriterLeaseUntil}}
	if alter != nil {
		alter(document)
	}
	digest := func(value any) string {
		raw, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(raw)
		return "sha256:" + hex.EncodeToString(sum[:])
	}
	document["payloadDigest"] = digest(document["payload"])
	document["digest"] = digest(document)
	unsigned, _ := json.Marshal(document)
	document["signature"] = base64.RawURLEncoding.EncodeToString(ed25519.Sign(private, append([]byte("deepseek-infra:control-agent-execution-grant-v1\x00"), unsigned...)))
	raw, _ := json.Marshal(document)
	return raw
}

func TestAgentExecutionAuthorizeRealStoreAndImmutableReplay(t *testing.T) {
	for _, phase := range []string{"plan", "tasks"} {
		t.Run(phase, func(t *testing.T) {
			control, service, _, input, _ := agentAuthorizeFixture(t, phase)
			session, _ := service.executionSigner(control)
			original := proto.Clone(input)
			first, err := service.AuthorizeExecution(context.Background(), input)
			if err != nil || first.AlreadyApplied || len(first.CanonicalAuthorization) == 0 || first.Claim.RunId != input.RunId {
				t.Fatalf("legitimate authorization failed: %v", err)
			}
			// The Rust journal returns original bytes on retry even when the Go
			// callback renews lease dates. Keep that property in this unit fixture.
			session.client = fixtureAgentAuthorizer(func(_ context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, _ worker.ControlSignerBinding) ([]byte, error) {
				intent, err := agentSigningIntent(claim)
				wantID, wantNonce := agentSigningIdentifiers(intent)
				if err != nil || id != wantID || nonce != wantNonce {
					t.Fatal("replay changed the immutable request identity")
				}
				return first.CanonicalAuthorization, nil
			})
			service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) { return session, nil }
			second, err := service.AuthorizeExecution(context.Background(), input)
			if err != nil || !second.AlreadyApplied || !bytes.Equal(first.CanonicalAuthorization, second.CanonicalAuthorization) || !proto.Equal(input, original) {
				t.Fatalf("authorization replay lost its scope/bytes or changed caller input: %v", err)
			}
			actions, err := control.ListAuthoritativeRecords("action")
			if err != nil || len(actions) != 1 || actions[0].State != "CLAIMED" {
				t.Fatal("signing created duplicate actions or fabricated effect completion")
			}
			run, found, err := control.AgentRunMetadata(input.RunId)
			if err != nil || !found || run.NextIndex != input.ExpectedIndex || run.Status == "done" {
				t.Fatal("signing fabricated Agent progress")
			}
		})
	}
}

func TestAgentExecutionAuthorizeRejectsChangedScopeAndSigningErrors(t *testing.T) {
	for _, fault := range []string{"plan-edit", "cancel-run", "cancel-context", "expired-action", "closed-store", "bad-signature", "wrong-request", "wrong-nonce", "wrong-body"} {
		t.Run(fault, func(t *testing.T) {
			var leaseClock atomic.Int64
			var now func() int64
			if fault == "expired-action" {
				leaseClock.Store(time.Now().UTC().Unix())
				now = leaseClock.Load
			}
			control, service, client, input, private := agentAuthorizeFixtureWithClock(t, "tasks", now)
			if fault == "expired-action" {
				input.LeaseSeconds = 1
			}
			session, _ := service.executionSigner(control)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			want := codes.DataLoss
			signerCalled := false
			session.client = fixtureAgentAuthorizer(func(_ context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
				signerCalled = true
				raw := fixtureAgentAuthorization(t, claim, id, nonce, binding, private, func(document map[string]any) {
					switch fault {
					case "wrong-request":
						document["requestId"] = strings.Repeat("9", 64)
					case "wrong-nonce":
						document["nonce"] = strings.Repeat("9", 64)
					case "wrong-body":
						document["payload"].(map[string]any)["requestSha256"] = strings.Repeat("9", 64)
					}
				})
				switch fault {
				case "plan-edit", "cancel-run":
					change := agentRPCAppend(input.ExpectedIndex, input.MetadataFence.ExecutionEpoch)
					if fault == "plan-edit" {
						change.Event.Type, change.Event.Status, change.Event.HasPlan = "agent_plan", "", true
						change.Event.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "changed task"}}
					} else {
						change.Event.Status = "cancelled"
					}
					if _, err := client.AppendEvent(controlRPCContext(t, testInternalBearer), change); err != nil {
						t.Fatal(err)
					}
					want = codes.FailedPrecondition
				case "cancel-context":
					cancel()
					want = codes.Canceled
				case "expired-action":
					// Keep admission live even under race instrumentation, then
					// expire the same claim before the post-signing renewal.
					leaseClock.Store(claim.LeaseUntil + 1)
					want = codes.FailedPrecondition
				case "closed-store":
					if err := control.Close(); err != nil {
						t.Fatal(err)
					}
					want = codes.FailedPrecondition
				case "bad-signature":
					var document map[string]any
					_ = json.Unmarshal(raw, &document)
					document["signature"] = strings.Repeat("a", 86)
					raw, _ = json.Marshal(document)
				}
				return raw, nil
			})
			service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) { return session, nil }
			response, err := service.AuthorizeExecution(ctx, input)
			if status.Code(err) != want || response != nil {
				t.Fatalf("unsafe authorization escaped: code=%s want=%s error=%v", status.Code(err), want, err)
			}
			if fault == "expired-action" && (!signerCalled || status.Convert(err).Message() != "ACTION_LEASE_EXPIRED") {
				t.Fatalf("expired claim did not reach the post-signing lease check: signerCalled=%t error=%v", signerCalled, err)
			}
		})
	}
	for _, code := range []codes.Code{codes.Canceled, codes.DeadlineExceeded, codes.FailedPrecondition, codes.InvalidArgument,
		codes.Unauthenticated, codes.PermissionDenied, codes.Unavailable, codes.Unknown} {
		t.Run(code.String(), func(t *testing.T) {
			control, service, _, input, _ := agentAuthorizeFixture(t, "plan")
			session, _ := service.executionSigner(control)
			session.client = fixtureAgentAuthorizer(func(context.Context, *agentv1.AgentRunExecutionClaim, string, string, worker.ControlSignerBinding) ([]byte, error) {
				return nil, status.Error(code, "offline diagnostic must not enter response")
			})
			service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) { return session, nil }
			response, err := service.AuthorizeExecution(context.Background(), input)
			want := code
			if code == codes.InvalidArgument {
				want = codes.FailedPrecondition
			}
			if code == codes.Unauthenticated {
				want = codes.PermissionDenied
			}
			if code == codes.Unknown {
				want = codes.Unavailable
			}
			if response != nil || status.Code(err) != want || strings.Contains(err.Error(), "offline diagnostic") {
				t.Fatalf("signing error escaped its control boundary: %v", err)
			}
		})
	}
}

func TestAgentExecutionAuthorizeAdmissionAndInputRefusals(t *testing.T) {
	control, service, _, input, _ := agentAuthorizeFixture(t, "plan")
	for _, shape := range []string{"nil", "version", "phase", "unknown", "fence-unknown", "missing-run", "bad-owner"} {
		t.Run(shape, func(t *testing.T) {
			bad := proto.Clone(input).(*agentv1.ClaimAgentRunExecutionRequest)
			want := codes.InvalidArgument
			switch shape {
			case "nil":
				bad = nil
			case "version":
				bad.SchemaVersion = 99
			case "phase":
				bad.Phase = 99
			case "unknown":
				bad.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 1})
			case "fence-unknown":
				bad.MetadataFence.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 1})
			case "missing-run":
				// Preserve ClaimExecution's refusal: a missing run is not
				// claimable, rather than a metadata lookup's NotFound result.
				bad.RunId, bad.MetadataFence.ActionId, want = "run_missing", "run_missing", codes.FailedPrecondition
			case "bad-owner":
				bad.Owner = ""
			}
			if _, err := service.AuthorizeExecution(context.Background(), bad); status.Code(err) != want ||
				(shape == "missing-run" && status.Convert(err).Message() != "ACTION_NOT_CLAIMABLE") {
				t.Fatalf("invalid admission: got=%s want=%s error=%v", status.Code(err), want, err)
			}
		})
	}
	service.health = ShadowHealth()
	if _, err := service.AuthorizeExecution(context.Background(), input); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("shadow process signed: %v", err)
	}
	service.health = HealthFor(config.Config{Mode: config.ModeAuthoritative})
	service.control = nil
	if _, err := service.AuthorizeExecution(context.Background(), input); status.Code(err) != codes.Unavailable {
		t.Fatal(err)
	}
	service.control = control
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := service.AuthorizeExecution(ctx, input); status.Code(err) != codes.Canceled {
		t.Fatal(err)
	}
	for _, fault := range []string{"error", "nil", "no-client", "bad-key"} {
		t.Run(fault, func(t *testing.T) {
			service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
				switch fault {
				case "error":
					return nil, errors.New("offline unavailable")
				case "nil":
					return nil, nil
				case "no-client":
					return &agentExecutionSignerSession{}, nil
				default:
					return &agentExecutionSignerSession{binding: worker.ControlSignerBinding{PublicKey: "invalid"},
						client: fixtureAgentAuthorizer(func(context.Context, *agentv1.AgentRunExecutionClaim, string, string, worker.ControlSignerBinding) ([]byte, error) {
							return nil, nil
						}), close: func() error { return nil }}, nil
				}
			}
			want := codes.Unavailable
			if fault == "bad-key" {
				want = codes.DataLoss
			}
			if _, err := service.AuthorizeExecution(context.Background(), input); status.Code(err) != want {
				t.Fatal(err)
			}
		})
	}
}

func TestAgentExecutionAuthorizeIdentityAndReconciliation(t *testing.T) {
	_, service, _, input, _ := agentAuthorizeFixture(t, "tasks")
	result, err := service.AuthorizeExecution(context.Background(), input)
	if err != nil {
		t.Fatal(err)
	}
	claim := result.Claim
	intent, err := agentSigningIntent(claim)
	if err != nil {
		t.Fatal(err)
	}
	id, nonce := agentSigningIdentifiers(intent)
	clone := proto.Clone(claim).(*agentv1.AgentRunExecutionClaim)
	clone.LeaseUntil++
	clone.WriterLeaseUntil++
	clone.State = "EXECUTING"
	other, err := agentSigningIntent(clone)
	otherID, otherNonce := agentSigningIdentifiers(other)
	if err != nil || id != otherID || nonce != otherNonce || id == nonce {
		t.Fatal("lease renewal changed immutable signing identity")
	}
	other.ClaimToken += "-changed"
	changedID, changedNonce := agentSigningIdentifiers(other)
	if changedID == id || changedNonce == nonce {
		t.Fatal("rebound claim reused signing identity")
	}
	for _, fault := range []string{"nil", "fence", "request", "reconcile", "unknown", "phase"} {
		bad := proto.Clone(claim).(*agentv1.AgentRunExecutionClaim)
		switch fault {
		case "nil":
			bad = nil
		case "fence":
			bad.Fence = nil
		case "request":
			bad.Request = nil
		case "reconcile":
			bad.ReconciliationRequired = true
		case "unknown":
			bad.State = "EFFECT_UNKNOWN"
		case "phase":
			bad.Phase = 99
		}
		if _, err := agentSigningIntent(bad); err == nil {
			t.Fatalf("invalid %s claim accepted", fault)
		}
	}
}

func TestAgentExecutionAuthorizeConfiguredTLSRealm(t *testing.T) {
	control := agentAuthorizeStore(t, 600)
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{31}, ed25519.SeedSize)) // offline unit fixture only
	public := base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "Offline Agent Test Root"},
		NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour), IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign}
	der, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	root := filepath.Join(t.TempDir(), "root.pem")
	if err := os.WriteFile(root, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	values := map[string]string{worker.EnvWorkerTarget: "127.0.0.1:65534", worker.EnvWorkerTLSCAFile: root,
		worker.EnvWorkerTLSServerName: "localhost", worker.EnvWorkerServiceBearer: "offline-test-" + strings.Repeat("x", 32),
		worker.EnvWorkerServiceBearerExpires:          now.Add(10 * time.Minute).Truncate(time.Second).Format(time.RFC3339),
		"DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY": public, "DEEPSEEK_WORKER_AUTHORITY_FLEET_ID": mutationFleetID,
		"DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT": mutationEnvironment}
	for key, value := range values {
		t.Setenv(key, value)
	}
	session, err := configuredAgentExecutionSigner(control)
	if err != nil || session == nil || session.close == nil || session.binding.PublicKey != public {
		t.Fatalf("valid pinned TLS configuration: %v", err)
	}
	if err := session.close(); err != nil {
		t.Fatal(err)
	}
	for _, fault := range []string{"remote", "bad-key", "fleet", "environment", "missing-root"} {
		t.Run(fault, func(t *testing.T) {
			for key, value := range values {
				t.Setenv(key, value)
			}
			switch fault {
			case "remote":
				t.Setenv(worker.EnvWorkerTarget, "192.0.2.1:65534")
			case "bad-key":
				t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", "invalid")
			case "fleet":
				t.Setenv("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", "other-fleet")
			case "environment":
				t.Setenv("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT", "other-environment")
			case "missing-root":
				t.Setenv(worker.EnvWorkerTLSCAFile, filepath.Join(t.TempDir(), "missing.pem"))
			}
			if _, err := configuredAgentExecutionSigner(control); err == nil {
				t.Fatalf("unsafe %s configuration accepted", fault)
			}
		})
	}
}
