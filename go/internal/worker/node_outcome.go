package worker

import (
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

var ErrAgentNodeOutcomeInvalid = agentproof.ErrAgentNodeOutcomeInvalid

const agentNodeOutcomeDomain = agentproof.NodeOutcomeDomain

type VerifiedAgentNodeOutcome = agentproof.VerifiedAgentNodeOutcome

func VerifyAgentNodeOutcome(receipt *agentv1.AgentNodeOutcomeReceipt, intent *agentv1.AgentNodeDispatchIntent,
	writerFencingToken int64, binding ControlSignerBinding) (*VerifiedAgentNodeOutcome, error) {
	return agentproof.VerifyAgentNodeOutcome(receipt, intent, writerFencingToken,
		agentproof.Binding{PublicKey: binding.PublicKey, FleetID: binding.FleetID, Environment: binding.Environment})
}

func VerifyAgentNodeEffect(effect *agentv1.AgentNodeEffectMetadata, intent *agentv1.AgentNodeDispatchIntent,
	writerFencingToken int64, binding ControlSignerBinding) (*VerifiedAgentNodeOutcome, error) {
	return agentproof.VerifyAgentNodeEffect(effect, intent, writerFencingToken,
		agentproof.Binding{PublicKey: binding.PublicKey, FleetID: binding.FleetID, Environment: binding.Environment})
}
