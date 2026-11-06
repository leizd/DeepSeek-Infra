package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"strings"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

const (
	CompactPatchOutcomeVersion = 2
	FlatPatchOutcomeVersion    = 4
)

var compactPatchSignatureDomain = []byte("deepseek-infra:media-patch-outcome-v2\x00")
var flatPatchSignatureDomain = []byte("deepseek-infra:media-patch-outcome-v4\x00")

func verifyCompactPatchOutcome(outcome *mediav1.MediaPatchOutcome, claim *mediav1.MediaPatchClaim, public []byte) error {
	if outcome == nil || claim == nil || claim.Intent == nil || claim.Fence == nil ||
		claim.SchemaVersion != 1 ||
		claim.Intent.Kind != mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED ||
		claim.Intent.Patch == nil || !strings.HasPrefix(outcome.OperationId, "media-patch-") ||
		outcome.OperationId != claim.Intent.OperationId ||
		outcome.OperationId != claim.Fence.ActionId || claim.Fence.ExecutionEpoch != 1 ||
		len(outcome.OperationId) > 128 || !proto.Equal(outcome.OriginalFence, claim.Fence) ||
		!bytes.Equal(outcome.RequestSha256, claim.RequestSha256) || len(claim.RequestSha256) != 32 ||
		len(public) != ed25519.PublicKeySize || len(outcome.ProtoReflect().GetUnknown()) != 0 ||
		len(claim.ProtoReflect().GetUnknown()) != 0 || len(claim.Fence.ProtoReflect().GetUnknown()) != 0 ||
		outcome.FleetId != claim.FleetId || outcome.Environment != claim.Environment ||
		claim.FleetId == "" || claim.Environment == "" || outcome.WriterFencingToken != claim.WriterFencingToken ||
		outcome.WriterFencingToken < 1 || outcome.State != mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED ||
		outcome.HttpStatus != 200 || outcome.ErrorCode != "" || outcome.ErrorMessage != "" ||
		len(outcome.BeforeSha256) != 32 || len(outcome.AfterSha256) != 32 ||
		outcome.Media != nil || outcome.Created != nil || outcome.Processed != nil || outcome.DeletedCount != 0 {
		return ErrInvalid
	}
	var preparationSchema uint32
	var domain []byte
	capability := claim.Intent.MaxOutcomeSchemaVersion
	switch outcome.SchemaVersion {
	case CompactPatchOutcomeVersion:
		if capability != CompactPatchOutcomeVersion && capability != ServerErrorOutcomeVersion && capability != FlatPatchOutcomeVersion {
			return ErrInvalid
		}
		preparationSchema, domain = 1, compactPatchSignatureDomain
	case FlatPatchOutcomeVersion:
		if capability != FlatPatchOutcomeVersion {
			return ErrInvalid
		}
		preparationSchema, domain = 2, flatPatchSignatureDomain
	default:
		return ErrInvalid
	}
	requestDigest, err := RequestDigest(claim.Intent)
	if err != nil || !bytes.Equal(requestDigest, claim.RequestSha256) {
		return ErrInvalid
	}
	receipt := outcome.Patched
	if receipt == nil || len(receipt.ProtoReflect().GetUnknown()) != 0 ||
		!proto.Equal(receipt.OriginalFence, claim.Fence) || len(receipt.RecordSha256) != 32 ||
		receipt.RecordBytes == 0 || len(receipt.PreparationSha256) != 32 {
		return ErrInvalid
	}
	original := proto.Clone(claim).(*mediav1.MediaPatchClaim)
	original.State = "CLAIMED"
	preparation, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&mediav1.MediaPatchPreparation{
		SchemaVersion: preparationSchema, Claim: original, RecordSha256: receipt.RecordSha256, RecordBytes: receipt.RecordBytes,
	})
	if err != nil {
		return ErrInvalid
	}
	digest := sha256.Sum256(preparation)
	if !bytes.Equal(digest[:], receipt.PreparationSha256) {
		return ErrInvalid
	}
	unsigned := proto.Clone(outcome).(*mediav1.MediaPatchOutcome)
	unsigned.Signature = nil
	encoded, err := Encode(unsigned)
	if err != nil || len(encoded) > 2*1024*1024 {
		return ErrInvalid
	}
	message := append(append([]byte(nil), domain...), encoded...)
	if !ed25519.Verify(public, message, outcome.Signature) {
		return ErrInvalid
	}
	return nil
}
