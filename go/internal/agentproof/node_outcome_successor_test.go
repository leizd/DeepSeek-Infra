package agentproof

import (
	"errors"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func TestAgentNodeOutcomeAcceptsMatchingSuccessorOperationID(t *testing.T) {
	effect, intent, binding, private := signedNodeFixture(t)
	successorID := "agent-successor-" + strings.Repeat("b", 64)
	intent.OperationId = successorID
	intent.Fence.ExecutionEpoch = 2
	effect.OperationId = successorID
	effect.Fence.ExecutionEpoch = 2
	effect.OutcomeReceipt.Evidence.OperationId = successorID
	effect.OutcomeReceipt.Evidence.Fence.ExecutionEpoch = 2
	resignNodeFixture(t, effect.OutcomeReceipt, private)

	verified, err := VerifyAgentNodeEffect(effect, intent, 7, binding)
	if err != nil {
		t.Fatalf("valid successor effect rejected: %v", err)
	}
	if verified.Evidence().OperationId != successorID || verified.Evidence().Fence.ExecutionEpoch != 2 {
		t.Fatalf("unexpected verified successor evidence: %+v", verified.Evidence())
	}

	// Cross-prefix rebinding must fail closed even when the intent is altered to match the signature or kept original.
	originalIntent := proto.Clone(intent).(*agentv1.AgentNodeDispatchIntent)
	originalIntent.OperationId = "agent-node-" + strings.Repeat("a", 64)
	if _, err := VerifyAgentNodeOutcome(effect.OutcomeReceipt, originalIntent, 7, binding); !errors.Is(err, ErrAgentNodeOutcomeInvalid) {
		t.Fatalf("successor receipt verified against original intent: %v", err)
	}
}

func TestAgentNodeOutcomeRejectsMalformedSuccessorOperationIDs(t *testing.T) {
	for name, badID := range map[string]string{
		"short digest":   "agent-successor-" + strings.Repeat("b", 63),
		"long digest":    "agent-successor-" + strings.Repeat("b", 65),
		"uppercase hex":  "agent-successor-" + strings.Repeat("B", 64),
		"non hex":        "agent-successor-" + strings.Repeat("g", 64),
		"unknown prefix": "agent-retry-" + strings.Repeat("b", 64),
	} {
		t.Run(name, func(t *testing.T) {
			effect, intent, binding, private := signedNodeFixture(t)
			intent.OperationId = badID
			effect.OperationId = badID
			effect.OutcomeReceipt.Evidence.OperationId = badID
			resignNodeFixture(t, effect.OutcomeReceipt, private)

			if _, err := VerifyAgentNodeOutcome(effect.OutcomeReceipt, intent, 7, binding); !errors.Is(err, ErrAgentNodeOutcomeInvalid) {
				t.Fatalf("accepted malformed operation id %q: %v", badID, err)
			}
		})
	}
}
