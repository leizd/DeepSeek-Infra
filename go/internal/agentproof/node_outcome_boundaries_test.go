package agentproof

import (
	"errors"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func TestAgentNodeEffectRefusesMissingOrUnconfirmedMetadata(t *testing.T) {
	if _, err := VerifyAgentNodeEffect(nil, nil, 0, Binding{}); !errors.Is(err, ErrAgentNodeOutcomeInvalid) {
		t.Fatalf("nil effect was accepted: %v", err)
	}
	pending := &agentv1.AgentNodeEffectMetadata{State: "PENDING"}
	if _, err := VerifyAgentNodeEffect(pending, nil, 0, Binding{}); !errors.Is(err, ErrAgentNodeOutcomeInvalid) {
		t.Fatalf("an unconfirmed effect was accepted: %v", err)
	}
}
