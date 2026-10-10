package worker

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
	"strings"
	"testing"
	"time"
)

func agentSigningFixture() (*actionv1.SignControlRequest, ControlSignerBinding, ed25519.PrivateKey) {
	request, binding, private := controlSigningFixture()
	now := time.Now().UTC().Truncate(time.Second).Unix()
	sum := sha256.Sum256([]byte("agent-run-execution-v1\nagent-run\nplan\n2\n"))
	request.Purpose = actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_AGENT_EXECUTION
	request.Fence.ActionId = "agent-exec-" + hex.EncodeToString(sum[:])
	request.AgentIntent = &agentv1.AgentRunExecutionClaim{Fence: proto.Clone(request.Fence).(*commonv1.ActionFence),
		RunId: "agent-run", Phase: agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN,
		Request: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 24}, PlanDigest: strings.Repeat("d", 64),
		MetadataIndex: 2, MetadataEpoch: 3, Owner: "rust-agent-worker", ClaimToken: "private-claim-token", ClaimRevision: 2,
		WriterFencingToken: 1, LeaseUntil: now + 60, WriterLeaseUntil: now + 120, State: "CLAIMED"}
	return request, binding, private
}
func fixtureAgentSigningResponse(t *testing.T, input *actionv1.SignControlRequest, binding ControlSignerBinding, private ed25519.PrivateKey, alter func(map[string]any)) *actionv1.SignControlResponse {
	t.Helper()
	claim := input.AgentIntent
	hash := func(domain, value string) string {
		sum := sha256.Sum256([]byte(domain + "\x00" + value))
		return "sha256:" + hex.EncodeToString(sum[:])
	}
	payload := map[string]any{"runId": claim.RunId, "phase": "plan", "requestSha256": claim.Request.Sha256, "requestLength": claim.Request.Length,
		"planDigest": claim.PlanDigest, "metadataIndex": claim.MetadataIndex, "metadataEpoch": claim.MetadataEpoch,
		"ownerDigest": hash("agent-owner-v1", claim.Owner), "claimDigest": hash("agent-claim-v1", claim.ClaimToken),
		"claimRevision": claim.ClaimRevision, "writerFencingToken": claim.WriterFencingToken, "leaseUntil": claim.LeaseUntil, "writerLeaseUntil": claim.WriterLeaseUntil}
	keyID, _ := store.SignerKeyIDForPublicKey(binding.PublicKey)
	document := map[string]any{"schema": store.AgentExecutionGrantSchema, "schemaVersion": 1, "domain": "action", "operation": store.AgentExecutionGrantOperation,
		"runtime": "go", "mode": "authoritative", "role": "control-plane", "fleetId": input.FleetId, "environment": input.Environment,
		"actionId": input.Fence.ActionId, "executionEpoch": input.Fence.ExecutionEpoch, "fencingToken": input.FencingToken, "revision": input.Revision,
		"requestId": input.RequestId, "nonce": input.Nonce, "issuedAt": time.Now().UTC().Truncate(time.Second).Format(time.RFC3339),
		"expiresAt": time.Unix(claim.LeaseUntil, 0).UTC().Format(time.RFC3339), "payload": payload, "signerKeyId": keyID, "signatureAlgorithm": "Ed25519"}
	if alter != nil {
		alter(document)
	}
	digest := func(value any) string {
		raw, _ := json.Marshal(value)
		sum := sha256.Sum256(raw)
		return "sha256:" + hex.EncodeToString(sum[:])
	}
	document["payloadDigest"] = digest(document["payload"])
	document["digest"] = digest(document)
	unsigned, _ := json.Marshal(document)
	document["signature"] = base64.RawURLEncoding.EncodeToString(ed25519.Sign(private, append([]byte("deepseek-infra:control-agent-execution-grant-v1\x00"), unsigned...)))
	raw, _ := json.Marshal(document)
	return &actionv1.SignControlResponse{Fence: proto.Clone(input.Fence).(*commonv1.ActionFence), CanonicalDocument: raw, SignerPublicKey: binding.PublicKey, SignerKeyId: keyID}
}
func TestAgentControlSignerVerifiesCurrentPhaseAndRejectsRebinding(t *testing.T) {
	input, binding, private := agentSigningFixture()
	client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(request *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		return fixtureAgentSigningResponse(t, input, binding, private, nil), nil
	})}
	raw, err := client.SignControl(context.Background(), input, binding)
	if err != nil || len(raw) == 0 {
		t.Fatalf("valid grant: %v", err)
	}
	original := proto.Clone(input)
	for _, mutate := range []func(*actionv1.SignControlRequest){
		func(r *actionv1.SignControlRequest) { r.AgentIntent.PlanDigest = strings.Repeat("9", 64) },
		func(r *actionv1.SignControlRequest) { r.AgentIntent.Request.Sha256 = strings.Repeat("9", 64) },
		func(r *actionv1.SignControlRequest) { r.AgentIntent.ReconciliationRequired = true },
		func(r *actionv1.SignControlRequest) { r.AgentIntent.State = "EFFECT_UNKNOWN" },
		func(r *actionv1.SignControlRequest) { r.AgentIntent.Phase = 99 },
		func(r *actionv1.SignControlRequest) {
			r.AgentIntent.Request.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 1})
		},
		func(r *actionv1.SignControlRequest) {
			r.Purpose = actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_INSTALL_EPOCH
		},
	} {
		bad := proto.Clone(input).(*actionv1.SignControlRequest)
		mutate(bad)
		if _, err := client.SignControl(context.Background(), bad, binding); err == nil {
			t.Fatal("rebound claim accepted")
		}
	}
	if !proto.Equal(original, input) {
		t.Fatal("caller claim mutated")
	}
}
func TestAuthorizeAgentExecutionInstallsEpochBeforeSigning(t *testing.T) {
	input, binding, private := agentSigningFixture()
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", binding.PublicKey)
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", binding.FleetID)
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT", binding.Environment)
	for _, mode := range []string{"success", "replay", "install-failure", "issuer-failure"} {
		t.Run(mode, func(t *testing.T) {
			rpc := &fakeWorkerRPC{installResp: &actionv1.InstallAuthoritativeEpochResponse{Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED, Fence: input.Fence}}
			if mode == "replay" {
				rpc.installResp = &actionv1.InstallAuthoritativeEpochResponse{Status: actionv1.AdmitStatus_ADMIT_STATUS_REJECTED, Error: &commonv1.ErrorDetail{Code: "AUTHORITY_REQUEST_REPLAY"}}
			}
			if mode == "install-failure" {
				rpc.installErr = context.DeadlineExceeded
			}
			calls := 0
			client := &Client{tlsSecured: true, bearerAttached: true, rpc: rpc, signer: fakeControlSigner(func(request *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
				calls++
				if request.Purpose == actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_AGENT_INSTALL_EPOCH {
					if !proto.Equal(request.AgentIntent, input.AgentIntent) {
						t.Fatal("Agent epoch lost its lease identity")
					}
					if mode == "issuer-failure" {
						return nil, context.DeadlineExceeded
					}
					return fixtureSigningResponse(t, request, binding, private, nil), nil
				}
				if rpc.installRequest == nil {
					t.Fatal("grant before epoch installation")
				}
				return fixtureAgentSigningResponse(t, request, binding, private, nil), nil
			})}
			before := proto.Clone(input.AgentIntent)
			raw, err := client.AuthorizeAgentExecution(context.Background(), input.AgentIntent, input.RequestId, input.Nonce, binding)
			if mode == "success" || mode == "replay" {
				if err != nil || len(raw) == 0 || calls != 2 {
					t.Fatalf("%s: %v", mode, err)
				}
			} else if err == nil {
				t.Fatal("failed installation accepted")
			}
			if !proto.Equal(before, input.AgentIntent) {
				t.Fatal("claim mutated")
			}
		})
	}
	if _, err := (&Client{}).AuthorizeAgentExecution(context.Background(), nil, "", "", binding); err == nil {
		t.Fatal("nil claim")
	}
	input.AgentIntent.Owner = ""
	if _, err := (&Client{}).AuthorizeAgentExecution(context.Background(), input.AgentIntent, input.RequestId, input.Nonce, binding); err == nil {
		t.Fatal("malformed claim")
	}
}

func TestAuthorizeAgentExecutionUsesLeaseBoundEpochPurpose(t *testing.T) {
	input, binding, private := agentSigningFixture()
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", binding.PublicKey)
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", binding.FleetID)
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT", binding.Environment)
	rpc := &fakeWorkerRPC{installResp: &actionv1.InstallAuthoritativeEpochResponse{
		Status: actionv1.AdmitStatus_ADMIT_STATUS_ADMITTED, Fence: input.Fence}}
	client := &Client{tlsSecured: true, bearerAttached: true, rpc: rpc,
		signer: fakeControlSigner(func(request *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
			if request.Purpose == actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_AGENT_EXECUTION {
				return fixtureAgentSigningResponse(t, request, binding, private, nil), nil
			}
			if request.Purpose != actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_AGENT_INSTALL_EPOCH || !proto.Equal(request.AgentIntent, input.AgentIntent) {
				t.Errorf("Agent epoch issuance did not carry its typed live claim: purpose=%d", request.Purpose)
			}
			return fixtureSigningResponse(t, request, binding, private, nil), nil
		})}
	if _, err := client.AuthorizeAgentExecution(context.Background(), input.AgentIntent,
		input.RequestId, input.Nonce, binding); err != nil {
		t.Fatal(err)
	}
}

func TestAgentEpochSigningAllowsOnlyFencedReconciliation(t *testing.T) {
	input, binding, private := agentSigningFixture()
	input.Purpose = actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_AGENT_INSTALL_EPOCH
	input.AgentIntent.State = "RECONCILING"
	input.AgentIntent.ReconciliationRequired = true
	client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(request *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		return fixtureSigningResponse(t, request, binding, private, nil), nil
	})}
	if _, err := client.SignControl(context.Background(), input, binding); err != nil {
		t.Fatalf("reconciliation fence: %v", err)
	}
	input.Purpose = actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_AGENT_EXECUTION
	if _, err := client.SignControl(context.Background(), input, binding); err == nil {
		t.Fatal("fresh effect during reconciliation")
	}
	input.Purpose = actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_AGENT_INSTALL_EPOCH
	input.AgentIntent.State = "EFFECT_UNKNOWN"
	if _, err := client.SignControl(context.Background(), input, binding); err == nil {
		t.Fatal("unsettled effect is not an authorized takeover")
	}
	input.AgentIntent.State = "RECONCILING"
	input.AgentIntent.ReconciliationRequired = false
	if _, err := client.SignControl(context.Background(), input, binding); err == nil {
		t.Fatal("inconsistent reconciliation claim")
	}
}
