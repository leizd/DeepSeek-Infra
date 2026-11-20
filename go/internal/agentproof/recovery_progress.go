package agentproof

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"math"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

const RecoveryProgressDomain = "deepseek-infra:agent-recovery-progress-proof-v1\x00"

var ErrAgentRecoveryProgressInvalid = errors.New("AGENT_RECOVERY_PROGRESS_INVALID")

func KnownRecoveryProgressMessage(message proto.Message) bool {
	return message != nil && recoveryProgressKnown(message.ProtoReflect())
}

func recoveryProgressKnown(message protoreflect.Message) bool {
	if !message.IsValid() || len(message.GetUnknown()) != 0 {
		return false
	}
	known := true
	message.Range(func(field protoreflect.FieldDescriptor, value protoreflect.Value) bool {
		if field.Kind() == protoreflect.MessageKind {
			if field.IsList() {
				for i := 0; i < value.List().Len(); i++ {
					if !recoveryProgressKnown(value.List().Get(i).Message()) {
						known = false
						break
					}
				}
			} else {
				known = recoveryProgressKnown(value.Message())
			}
		}
		return known
	})
	return known
}

// This proof admits public progress only. Original selected task outcomes and
// their independent terminal/resource journal must still be verified by Go.
func VerifyAgentRecoveryProgress(receipt *agentv1.AgentRecoveryProgressReceipt, expected *agentv1.AgentRecoveryProgressContext, binding Binding) error {
	invalid := ErrAgentRecoveryProgressInvalid
	if receipt == nil || expected == nil || receipt.SchemaVersion != 1 || receipt.Evidence == nil ||
		!recoveryProgressKnown(receipt.ProtoReflect()) || len(receipt.Signature) != ed25519.SignatureSize || receipt.SignerPublicKey != binding.PublicKey {
		return invalid
	}
	evidence := receipt.Evidence
	context := evidence.Context
	if evidence.SchemaVersion != 1 || context == nil || context.SchemaVersion != 1 || context.RunId == "" ||
		context.Generation == 0 || context.Generation > math.MaxInt64 || context.MetadataEpoch == 0 || context.MetadataEpoch > math.MaxInt64 ||
		context.ExpectedIndex < 1 || context.ExpectedIndex > math.MaxInt64-2 || context.RuntimeRevision < 2 || context.RuntimeRevision > math.MaxInt64 ||
		!ValidNodeOutcomeArtifact(context.Request) || !ValidNodeOutcomeDigest(context.RecoveryContextSha256) ||
		context.TaskActionId == "" || context.TaskActionRevision < 1 || context.TaskMetadataIndex != context.ExpectedIndex ||
		context.Role == "" || len(context.Outputs) != 1 || !ValidNodeOutcomeArtifact(evidence.NoteBody) || !ValidNodeOutcomeArtifact(evidence.NextBody) ||
		binding.FleetID == "" || binding.Environment == "" || evidence.FleetId != binding.FleetID || evidence.Environment != binding.Environment ||
		!proto.Equal(context, expected) {
		return invalid
	}
	key, err := base64.RawURLEncoding.DecodeString(binding.PublicKey)
	if err != nil || len(key) != ed25519.PublicKeySize || base64.RawURLEncoding.EncodeToString(key) != binding.PublicKey {
		return invalid
	}
	payload, err := (proto.MarshalOptions{Deterministic: true}).Marshal(evidence)
	if err != nil || len(payload) > 1<<20 || !ed25519.Verify(key, append([]byte(RecoveryProgressDomain), payload...), receipt.Signature) {
		return invalid
	}
	return nil
}
