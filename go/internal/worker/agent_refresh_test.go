package worker

import (
	"context"
	"errors"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func TestAgentRefreshSignsLiveClaimWithoutInstallingAnotherEpoch(t *testing.T) {
	input, binding, private := agentSigningFixture()
	original := proto.Clone(input.AgentIntent)
	calls := 0
	// No ActionWorker RPC is configured: refresh must only use the custody signer.
	client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(request *actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		calls++
		if request.Purpose != actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_AGENT_EXECUTION || !proto.Equal(request.AgentIntent, original) {
			t.Fatal("refresh substituted epoch installation or another claim")
		}
		response := fixtureAgentSigningResponse(t, request, binding, private, nil)
		request.AgentIntent.Owner = "mutated-signing-transport-input"
		return response, nil
	})}
	if raw, err := client.RefreshAgentExecution(context.Background(), input.AgentIntent, input.RequestId, input.Nonce, binding); err != nil || len(raw) == 0 {
		t.Fatal("refresh signing failed", err)
	}
	if calls != 1 || !proto.Equal(input.AgentIntent, original) {
		t.Fatal("signing aliased the caller claim or dispatched more than once")
	}
}

func TestAgentRefreshRefusesMissingOrMalformedClaimAndSignerFailure(t *testing.T) {
	input, binding, _ := agentSigningFixture()
	client := &Client{tlsSecured: true, bearerAttached: true, signer: fakeControlSigner(func(*actionv1.SignControlRequest) (*actionv1.SignControlResponse, error) {
		return nil, context.DeadlineExceeded
	})}
	if _, err := client.RefreshAgentExecution(context.Background(), nil, input.RequestId, input.Nonce, binding); !errors.Is(err, ErrControlSignerResponse) {
		t.Fatal(err)
	}
	malformed := proto.Clone(input.AgentIntent).(*agentv1.AgentRunExecutionClaim)
	malformed.Request = nil
	if _, err := client.RefreshAgentExecution(context.Background(), malformed, input.RequestId, input.Nonce, binding); !errors.Is(err, ErrControlSignerResponse) {
		t.Fatal(err)
	}
	if _, err := client.RefreshAgentExecution(context.Background(), input.AgentIntent, input.RequestId, input.Nonce, binding); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("signer transport failure was accepted", err)
	}
}
