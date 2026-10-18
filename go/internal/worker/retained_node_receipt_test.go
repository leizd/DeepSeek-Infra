package worker

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type retainedReceiptRPC struct {
	agentv1.AgentNodeWorkerClient
	result                *agentv1.QueryAgentNodeEffectResponse
	err                   error
	readCalls, queryCalls int
}

func (r *retainedReceiptRPC) ReadRetainedEffect(context.Context, *agentv1.QueryAgentNodeEffectRequest, ...grpc.CallOption) (*agentv1.QueryAgentNodeEffectResponse, error) {
	r.readCalls++
	return r.result, r.err
}
func (r *retainedReceiptRPC) QueryEffect(context.Context, *agentv1.QueryAgentNodeEffectRequest, ...grpc.CallOption) (*agentv1.QueryAgentNodeEffectResponse, error) {
	r.queryCalls++
	return nil, status.Error(codes.Internal, "unexpected attestation path")
}

func TestKnownReceiptQueryRequiresDedicatedReadAndNeverFallsBackToAttestation(t *testing.T) {
	effect, intent, _, _ := signedNodeFixture(t)
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(effect.OutcomeReceipt)
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(raw)
	query := &agentv1.QueryAgentNodeEffectRequest{OperationId: intent.OperationId, Fence: intent.Fence, RunId: intent.RunId, NodeId: intent.NodeId, ExpectedReceiptSha256: hex.EncodeToString(hash[:])}
	rpc := &retainedReceiptRPC{result: &agentv1.QueryAgentNodeEffectResponse{Found: true, Effect: effect}}
	client := &Client{nodes: rpc, tlsSecured: true, bearerAttached: true}
	actual, err := client.QueryAgentNodeEffect(context.Background(), query)
	if err != nil || !proto.Equal(actual, effect) || rpc.readCalls != 1 || rpc.queryCalls != 0 {
		t.Fatalf("original receipt was not read exactly: %v", err)
	}
	for _, problem := range []string{"old-server", "lost-custody", "different-document", "no-document", "malformed-pin"} {
		t.Run(problem, func(t *testing.T) {
			input := proto.Clone(query).(*agentv1.QueryAgentNodeEffectRequest)
			response := proto.Clone(rpc.result).(*agentv1.QueryAgentNodeEffectResponse)
			broken := &retainedReceiptRPC{result: response}
			switch problem {
			case "old-server":
				broken.err = status.Error(codes.Unimplemented, "retained read unavailable")
			case "lost-custody":
				broken.err = status.Error(codes.FailedPrecondition, "AGENT_RECOVERY_RECEIPT_NOT_RETAINED")
			case "different-document":
				input.ExpectedReceiptSha256 = strings.Repeat("f", 64)
			case "no-document":
				response.Found = false
				response.Effect = nil
			case "malformed-pin":
				input.ExpectedReceiptSha256 = "unconfirmed"
			}
			c := &Client{nodes: broken, tlsSecured: true, bearerAttached: true}
			if _, err := c.QueryAgentNodeEffect(context.Background(), input); err == nil || broken.queryCalls != 0 {
				t.Fatal("missing custody fell back to signing or unproved response", err)
			}
		})
	}
}
