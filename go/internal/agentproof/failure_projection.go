package agentproof

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"math"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

const FailureProjectionDomain = "deepseek-infra:agent-failure-projection-proof-v1\x00"

var ErrAgentFailureProjectionInvalid = errors.New("AGENT_FAILURE_PROJECTION_INVALID")

// Go independently supplies the original signed failure body and every current
// settlement field. The failure-purpose signature never authorizes a provider call.
func VerifyAgentFailureProjection(receipt *agentv1.AgentFailureProjectionReceipt, expected *agentv1.AgentFailureProjectionEvidence, binding Binding) error {
	invalid := ErrAgentFailureProjectionInvalid
	if receipt == nil || expected == nil || receipt.SchemaVersion != 1 || receipt.Evidence == nil ||
		len(receipt.ProtoReflect().GetUnknown()) != 0 || len(receipt.Evidence.ProtoReflect().GetUnknown()) != 0 ||
		len(receipt.Signature) != ed25519.SignatureSize || receipt.SignerPublicKey != binding.PublicKey {
		return invalid
	}
	e := receipt.Evidence
	if e.SchemaVersion != 1 || !ValidNodeOutcomeDigest(e.SourceReceiptSha256) || e.SettlementFence == nil ||
		len(e.SettlementFence.ProtoReflect().GetUnknown()) != 0 || e.SettlementFence.ActionId == "" || e.SettlementFence.ExecutionEpoch == 0 ||
		e.SettlementWriterToken <= 0 || e.RunId == "" || e.MetadataEpoch == 0 || e.ExpectedIndex < 0 || e.ExpectedIndex == math.MaxInt64 ||
		!ValidNodeOutcomeArtifact(e.ErrorBody) ||
		(e.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN && e.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS) ||
		binding.FleetID == "" || binding.Environment == "" || e.FleetId != binding.FleetID || e.Environment != binding.Environment {
		return invalid
	}
	if !proto.Equal(expected, e) {
		return invalid
	}
	key, err := base64.RawURLEncoding.DecodeString(binding.PublicKey)
	if err != nil || len(key) != ed25519.PublicKeySize || base64.RawURLEncoding.EncodeToString(key) != binding.PublicKey {
		return invalid
	}
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(e)
	if err != nil || len(payload) > 1024*1024 || !ed25519.Verify(key, append([]byte(FailureProjectionDomain), payload...), receipt.Signature) {
		return invalid
	}
	return nil
}

// Ordinary failed tasks still settle as task outputs. Only complete documented
// planner/synthesis rejections use this separate terminal failure primitive.
func ValidTerminalFailure(e *agentv1.AgentNodeOutcomeEvidence) bool {
	if e == nil || e.Failure == nil || len(e.ProviderPlan) != 0 || e.TaskOutput != nil {
		return false
	}
	f := e.Failure
	if f.SchemaVersion != 1 || len(f.ProtoReflect().GetUnknown()) != 0 || !ValidNodeOutcomeDigest(f.ResponseSha256) {
		return false
	}
	switch f.HttpStatus {
	case 400, 401, 402, 422, 429:
	default:
		return false
	}
	return e.Phase == agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN && e.NodeId == "leader" ||
		e.Phase == agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS && e.NodeId == "synthesizer"
}
