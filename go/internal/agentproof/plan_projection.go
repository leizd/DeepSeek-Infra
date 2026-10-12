package agentproof

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

const PlanProjectionDomain = "deepseek-infra:agent-plan-projection-proof-v1\x00"

var ErrAgentPlanProjectionInvalid = errors.New("AGENT_PLAN_PROJECTION_INVALID")

// The expected scheduling metadata is independently derived by Go from the
// already verified source receipt and its own run. Only Rust owns body bytes.
func VerifyAgentPlanProjection(receipt *agentv1.AgentPlanProjectionReceipt,
	expected *agentv1.AgentPlanProjectionEvidence, binding Binding) error {
	if receipt == nil || expected == nil || receipt.SchemaVersion != 1 || receipt.Evidence == nil ||
		len(receipt.ProtoReflect().GetUnknown()) != 0 || len(receipt.Evidence.ProtoReflect().GetUnknown()) != 0 ||
		len(receipt.Signature) != ed25519.SignatureSize || receipt.SignerPublicKey != binding.PublicKey {
		return ErrAgentPlanProjectionInvalid
	}
	evidence := receipt.Evidence
	if evidence.PlanningRequired && !ValidNodeOutcomeArtifact(evidence.PlanningBody) || !evidence.PlanningRequired && evidence.PlanningBody != nil {
		return ErrAgentPlanProjectionInvalid
	}
	if evidence.SchemaVersion != 1 || !ValidNodeOutcomeDigest(evidence.SourceReceiptSha256) ||
		evidence.SettlementFence == nil || len(evidence.SettlementFence.ProtoReflect().GetUnknown()) != 0 ||
		evidence.SettlementFence.ActionId == "" || evidence.SettlementFence.ExecutionEpoch == 0 ||
		evidence.SettlementWriterToken <= 0 || evidence.RunId == "" || evidence.MetadataEpoch == 0 || evidence.ExpectedIndex < 0 ||
		(evidence.NextStatus != "running" && evidence.NextStatus != "awaiting_plan") ||
		len(evidence.NormalizedPlan) == 0 || len(evidence.NormalizedPlan) > 32 ||
		!ValidNodeOutcomeArtifact(evidence.PlanBody) || !ValidNodeOutcomeArtifact(evidence.StatusBody) ||
		evidence.FleetId != binding.FleetID || evidence.Environment != binding.Environment ||
		binding.FleetID == "" || binding.Environment == "" {
		return ErrAgentPlanProjectionInvalid
	}
	for _, node := range evidence.NormalizedPlan {
		if node == nil || len(node.ProtoReflect().GetUnknown()) != 0 || node.Id == "" || len(node.Id) > 64 || len(node.Task) > 64*1024 {
			return ErrAgentPlanProjectionInvalid
		}
	}
	wanted := proto.Clone(expected).(*agentv1.AgentPlanProjectionEvidence)
	wanted.PlanBody = proto.Clone(evidence.PlanBody).(*agentv1.AgentArtifactReference)
	wanted.StatusBody = proto.Clone(evidence.StatusBody).(*agentv1.AgentArtifactReference)
	if evidence.PlanningBody != nil {
		wanted.PlanningBody = proto.Clone(evidence.PlanningBody).(*agentv1.AgentArtifactReference)
	}
	if !proto.Equal(wanted, evidence) {
		return ErrAgentPlanProjectionInvalid
	}
	key, err := base64.RawURLEncoding.DecodeString(binding.PublicKey)
	if err != nil || len(key) != ed25519.PublicKeySize || base64.RawURLEncoding.EncodeToString(key) != binding.PublicKey {
		return ErrAgentPlanProjectionInvalid
	}
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(evidence)
	if err != nil || len(payload) > 1024*1024 || !ed25519.Verify(key, append([]byte(PlanProjectionDomain), payload...), receipt.Signature) {
		return ErrAgentPlanProjectionInvalid
	}
	return nil
}
