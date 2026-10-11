package agentproof

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"strings"
	"time"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

type Binding struct{ PublicKey, FleetID, Environment string }

var ErrAgentNodeOutcomeInvalid = errors.New("AGENT_NODE_OUTCOME_INVALID")

const NodeOutcomeDomain = "deepseek-infra:agent-node-outcome-proof-v1\x00"

// A verified historical result grants no lease, dispatch, or phase transition.
// The evidence is private so an RPC caller cannot manufacture a verified flag.
type VerifiedAgentNodeOutcome struct {
	evidence *agentv1.AgentNodeOutcomeEvidence
}

func (outcome *VerifiedAgentNodeOutcome) Evidence() *agentv1.AgentNodeOutcomeEvidence {
	if outcome == nil || outcome.evidence == nil {
		return nil
	}
	return proto.Clone(outcome.evidence).(*agentv1.AgentNodeOutcomeEvidence)
}

// ValidNodeOutcomeDigest accepts canonical lowercase SHA-256 metadata only.
func ValidNodeOutcomeDigest(value string) bool {
	if len(value) != 64 {
		return false
	}
	for _, character := range value {
		if !(character >= '0' && character <= '9' || character >= 'a' && character <= 'f') {
			return false
		}
	}
	return true
}

// ValidNodeOutcomeArtifact rejects unknown fields and unbounded body references.
func ValidNodeOutcomeArtifact(reference *agentv1.AgentArtifactReference) bool {
	return reference != nil && len(reference.ProtoReflect().GetUnknown()) == 0 && ValidNodeOutcomeDigest(reference.Sha256) && reference.Length > 0 && reference.Length <= 128*1024*1024
}

// The expected intent and original writer token come from Go's durable admitted
// scope. References, a CONFIRMED string, and a successful RPC are insufficient.
func VerifyAgentNodeOutcome(receipt *agentv1.AgentNodeOutcomeReceipt, intent *agentv1.AgentNodeDispatchIntent,
	writerFencingToken int64, binding Binding) (*VerifiedAgentNodeOutcome, error) {
	invalid := func() (*VerifiedAgentNodeOutcome, error) { return nil, ErrAgentNodeOutcomeInvalid }
	if receipt == nil || receipt.Evidence == nil || intent == nil || intent.SchemaVersion != 1 || intent.RecordedAt <= 0 || intent.Fence == nil || intent.Request == nil ||
		receipt.SchemaVersion != 1 || len(receipt.ProtoReflect().GetUnknown()) != 0 || len(receipt.Signature) != ed25519.SignatureSize ||
		receipt.SignerPublicKey != binding.PublicKey || binding.FleetID == "" || binding.Environment == "" || writerFencingToken <= 0 {
		return invalid()
	}
	evidence := receipt.Evidence
	if evidence.SchemaVersion != 1 || len(evidence.ProtoReflect().GetUnknown()) != 0 || evidence.Fence == nil ||
		len(evidence.Fence.ProtoReflect().GetUnknown()) != 0 || !proto.Equal(evidence.Fence, intent.Fence) ||
		evidence.OperationId != intent.OperationId || !strings.HasPrefix(evidence.OperationId, "agent-node-") || len(evidence.OperationId) != 75 ||
		!ValidNodeOutcomeDigest(evidence.OperationId[11:]) || evidence.RunId == "" || evidence.RunId != intent.RunId ||
		evidence.NodeId == "" || evidence.NodeId != intent.NodeId || evidence.Phase != intent.Phase ||
		(evidence.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN && evidence.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS) ||
		(evidence.Phase == agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN && evidence.NodeId != "leader") ||
		!ValidNodeOutcomeArtifact(evidence.Request) || !proto.Equal(evidence.Request, intent.Request) || !ValidNodeOutcomeArtifact(evidence.PublicEvent) ||
		!ValidNodeOutcomeDigest(evidence.PlanDigest) || evidence.PlanDigest != intent.PlanDigest || evidence.MetadataIndex < 0 ||
		evidence.MetadataIndex != intent.MetadataIndex || evidence.MetadataEpoch == 0 || evidence.MetadataEpoch != intent.MetadataEpoch ||
		evidence.WriterFencingToken != writerFencingToken || !ValidNodeOutcomeDigest(evidence.OutcomeSha256) || evidence.OutcomeLength == 0 ||
		evidence.OutcomeLength > 1024*1024 || evidence.FleetId != binding.FleetID || evidence.Environment != binding.Environment ||
		evidence.Fence.ActionId == "" || evidence.Fence.ExecutionEpoch == 0 {
		return invalid()
	}
	created, err := time.Parse("2006-01-02T15:04:05Z", evidence.CreatedAt)
	if err != nil || created.Format("2006-01-02T15:04:05Z") != evidence.CreatedAt {
		return invalid()
	}
	confirmed, err := time.Parse("2006-01-02T15:04:05Z", evidence.ConfirmedAt)
	if err != nil || confirmed.Format("2006-01-02T15:04:05Z") != evidence.ConfirmedAt || confirmed.Before(created) || created.Unix() < intent.RecordedAt-30 || confirmed.After(time.Now().UTC().Add(30*time.Second)) {
		return invalid()
	}
	for _, candidate := range evidence.ProviderPlan {
		if candidate == nil || len(candidate.ProtoReflect().GetUnknown()) != 0 {
			return invalid()
		}
	}
	key, err := base64.RawURLEncoding.DecodeString(binding.PublicKey)
	if err != nil || len(key) != ed25519.PublicKeySize || base64.RawURLEncoding.EncodeToString(key) != binding.PublicKey {
		return invalid()
	}
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(evidence)
	if err != nil || len(payload) > 1024*1024 || !ed25519.Verify(ed25519.PublicKey(key), append([]byte(NodeOutcomeDomain), payload...), receipt.Signature) {
		return invalid()
	}
	return &VerifiedAgentNodeOutcome{evidence: proto.Clone(evidence).(*agentv1.AgentNodeOutcomeEvidence)}, nil
}

// RPC metadata is checked against the signed result as well as Go's admitted
// intent. A valid receipt cannot be attached to a different public projection.
func VerifyAgentNodeEffect(effect *agentv1.AgentNodeEffectMetadata, intent *agentv1.AgentNodeDispatchIntent,
	writerFencingToken int64, binding Binding) (*VerifiedAgentNodeOutcome, error) {
	if effect == nil || effect.State != "CONFIRMED" || len(effect.ProtoReflect().GetUnknown()) != 0 {
		return nil, ErrAgentNodeOutcomeInvalid
	}
	outcome, err := VerifyAgentNodeOutcome(effect.OutcomeReceipt, intent, writerFencingToken, binding)
	if err != nil {
		return nil, err
	}
	evidence := outcome.evidence
	if effect.OperationId != evidence.OperationId || !proto.Equal(effect.Fence, evidence.Fence) || effect.RunId != evidence.RunId ||
		effect.NodeId != evidence.NodeId || !proto.Equal(effect.PublicEvent, evidence.PublicEvent) ||
		effect.OutcomeSha256 != evidence.OutcomeSha256 || effect.OutcomeLength != evidence.OutcomeLength {
		return nil, ErrAgentNodeOutcomeInvalid
	}
	return outcome, nil
}
