package media

import (
	"bytes"
	"crypto/ed25519"
	"strings"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

const ServerErrorOutcomeVersion = 3

var serverErrorSignatureDomain = []byte("deepseek-infra:media-patch-outcome-v3\x00")

func serverErrorNegotiated(intent *mediav1.MediaPatchIntent) bool {
	return intent != nil &&
		(intent.MaxOutcomeSchemaVersion == ServerErrorOutcomeVersion || intent.MaxOutcomeSchemaVersion == FlatPatchOutcomeVersion) &&
		(intent.Kind == mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED ||
			intent.Kind == mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE)
}

func verifyServerErrorOutcome(outcome *mediav1.MediaPatchOutcome, claim *mediav1.MediaPatchClaim, public []byte) error {
	if claim == nil || !serverErrorNegotiated(claim.Intent) || claim.Fence == nil ||
		claim.SchemaVersion != 1 || len(claim.ProtoReflect().GetUnknown()) != 0 ||
		len(claim.Fence.ProtoReflect().GetUnknown()) != 0 ||
		outcome.OperationId != claim.Intent.OperationId || outcome.OperationId != claim.Fence.ActionId ||
		len(outcome.OperationId) > 128 || claim.Fence.ExecutionEpoch != 1 ||
		!proto.Equal(outcome.OriginalFence, claim.Fence) ||
		len(outcome.ProtoReflect().GetUnknown()) != 0 || len(public) != ed25519.PublicKeySize ||
		len(claim.RequestSha256) != 32 || !bytes.Equal(outcome.RequestSha256, claim.RequestSha256) ||
		claim.FleetId == "" || claim.Environment == "" || outcome.FleetId != claim.FleetId ||
		outcome.Environment != claim.Environment || outcome.WriterFencingToken != claim.WriterFencingToken ||
		outcome.WriterFencingToken < 1 || len(outcome.BeforeSha256) != 32 ||
		outcome.HttpStatus != 500 || outcome.ErrorCode != "internal" || outcome.ErrorMessage != "Server error" ||
		outcome.Media != nil || outcome.Created != nil || outcome.Processed != nil ||
		outcome.Patched != nil || outcome.DeletedCount != 0 {
		return ErrInvalid
	}
	patch := claim.Intent.Kind == mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED
	prefix := "media-delete-"
	if patch {
		prefix = "media-patch-"
	}
	if !strings.HasPrefix(outcome.OperationId, prefix) {
		return ErrInvalid
	}
	switch outcome.State {
	case mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED:
		if !patch || len(outcome.AfterSha256) != 32 || bytes.Equal(outcome.AfterSha256, outcome.BeforeSha256) {
			return ErrInvalid
		}
	case mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED:
		if len(outcome.AfterSha256) != 0 {
			return ErrInvalid
		}
	default:
		return ErrInvalid
	}
	digest, err := RequestDigest(claim.Intent)
	if err != nil || !bytes.Equal(digest, claim.RequestSha256) {
		return ErrInvalid
	}
	unsigned := proto.Clone(outcome).(*mediav1.MediaPatchOutcome)
	unsigned.Signature = nil
	encoded, err := Encode(unsigned)
	if err != nil || len(encoded) > 2*1024*1024 {
		return ErrInvalid
	}
	message := append(append([]byte(nil), serverErrorSignatureDomain...), encoded...)
	if !ed25519.Verify(public, message, outcome.Signature) {
		return ErrInvalid
	}
	return nil
}
