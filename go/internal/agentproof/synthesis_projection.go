package agentproof

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"math"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

const SynthesisProjectionDomain = "deepseek-infra:agent-synthesis-projection-proof-v1\x00"

var ErrAgentSynthesisProjectionInvalid = errors.New("AGENT_SYNTHESIS_PROJECTION_INVALID")

// Go supplies every expected scope field and the confirmed source's content
// reference. Rust alone derives the done body and holds the signing key.
func VerifyAgentSynthesisProjection(receipt *agentv1.AgentSynthesisProjectionReceipt, expected *agentv1.AgentSynthesisProjectionEvidence, binding Binding) error {
	invalid := ErrAgentSynthesisProjectionInvalid
	if receipt == nil || expected == nil || receipt.SchemaVersion != 1 || receipt.Evidence == nil ||
		len(receipt.ProtoReflect().GetUnknown()) != 0 || len(receipt.Evidence.ProtoReflect().GetUnknown()) != 0 ||
		len(receipt.Signature) != ed25519.SignatureSize || receipt.SignerPublicKey != binding.PublicKey {
		return invalid
	}
	e := receipt.Evidence
	if e.SchemaVersion != 1 || !ValidNodeOutcomeDigest(e.SourceReceiptSha256) || e.SettlementFence == nil ||
		len(e.SettlementFence.ProtoReflect().GetUnknown()) != 0 || e.SettlementFence.ActionId == "" || e.SettlementFence.ExecutionEpoch == 0 ||
		e.SettlementWriterToken <= 0 || e.RunId == "" || e.MetadataEpoch == 0 || e.ExpectedIndex < 0 || e.ExpectedIndex > math.MaxInt64-2 ||
		!ValidNodeOutcomeArtifact(e.ContentBody) || !ValidNodeOutcomeArtifact(e.DoneBody) ||
		binding.FleetID == "" || binding.Environment == "" || e.FleetId != binding.FleetID || e.Environment != binding.Environment {
		return invalid
	}
	wanted := proto.Clone(expected).(*agentv1.AgentSynthesisProjectionEvidence)
	wanted.DoneBody = proto.Clone(e.DoneBody).(*agentv1.AgentArtifactReference)
	if !proto.Equal(wanted, e) {
		return invalid
	}
	key, err := base64.RawURLEncoding.DecodeString(binding.PublicKey)
	if err != nil || len(key) != ed25519.PublicKeySize || base64.RawURLEncoding.EncodeToString(key) != binding.PublicKey {
		return invalid
	}
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(e)
	if err != nil || len(payload) > 1024*1024 || !ed25519.Verify(key, append([]byte(SynthesisProjectionDomain), payload...), receipt.Signature) {
		return invalid
	}
	return nil
}
