package media

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"testing"

	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

func compactPatchFixture(t *testing.T) (*mediav1.MediaPatchOutcome, *mediav1.MediaPatchClaim, ed25519.PrivateKey) {
	t.Helper()
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	intent := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-patch-compact", MediaId: "media_one", Patch: &mediav1.MediaPatch{Title: &mediav1.MediaValue{Kind: &mediav1.MediaValue_TextValue{TextValue: "Updated"}}}, MaxOutcomeSchemaVersion: 2}
	request, err := RequestDigest(intent)
	if err != nil {
		t.Fatal(err)
	}
	fence := &commonv1.ActionFence{ActionId: intent.OperationId, ExecutionEpoch: 1}
	claim := &mediav1.MediaPatchClaim{SchemaVersion: 1, Intent: intent, Fence: fence, RequestSha256: request, Owner: "writer", ClaimToken: "token", WriterFencingToken: 3, LeaseUntilUnix: 1, State: "CLAIMED", FleetId: "fleet-a", Environment: "test"}
	record := sha256.Sum256([]byte("retained typed bytes"))
	preparation, err := proto.MarshalOptions{Deterministic: true}.Marshal(&mediav1.MediaPatchPreparation{SchemaVersion: 1, Claim: claim, RecordSha256: record[:], RecordBytes: 19})
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(preparation)
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 2, OperationId: intent.OperationId, OriginalFence: proto.Clone(fence).(*commonv1.ActionFence), RequestSha256: request, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, HttpStatus: 200, BeforeSha256: record[:], AfterSha256: record[:], FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken, Patched: &mediav1.MediaPatchedResult{RecordSha256: record[:], RecordBytes: 19, PreparationSha256: digest[:], OriginalFence: proto.Clone(fence).(*commonv1.ActionFence)}}
	signCompactPatch(t, outcome, key, compactPatchSignatureDomain)
	return outcome, claim, key
}

func signCompactPatch(t *testing.T, o *mediav1.MediaPatchOutcome, key ed25519.PrivateKey, domain []byte) {
	t.Helper()
	o.Signature = nil
	encoded, err := Encode(o)
	if err != nil {
		t.Fatal(err)
	}
	o.Signature = ed25519.Sign(key, append(append([]byte(nil), domain...), encoded...))
}

func TestCompactPatchOutcomeBindsOriginalClaimAndUsesDistinctDomain(t *testing.T) {
	o, c, key := compactPatchFixture(t)
	public := key.Public().(ed25519.PublicKey)
	for _, state := range []string{"CLAIMED", "DISPATCHED", "APPLIED", "EFFECT_UNKNOWN"} {
		c.State = state
		if err := VerifyOutcome(o, c, public); err != nil {
			t.Fatalf("historical state %s: %v", state, err)
		}
	}
	signCompactPatch(t, o, key, signatureDomain)
	if VerifyOutcome(o, c, public) == nil {
		t.Fatal("v1 signature accepted for compact v2")
	}
	o.SchemaVersion = 1
	signCompactPatch(t, o, key, signatureDomain)
	if VerifyOutcome(o, c, public) == nil {
		t.Fatal("compact field accepted by frozen v1")
	}
}

func TestCompactPatchCapabilityIsExplicitAndCannotAuthorizeOtherKinds(t *testing.T) {
	_, claim, _ := compactPatchFixture(t)
	for _, version := range []uint32{0, 2, ServerErrorOutcomeVersion, FlatPatchOutcomeVersion, 1, 5} {
		intent := proto.Clone(claim.Intent).(*mediav1.MediaPatchIntent)
		intent.MaxOutcomeSchemaVersion = version
		_, err := RequestDigest(intent)
		if (version == 0 || version == 2 || version == ServerErrorOutcomeVersion || version == FlatPatchOutcomeVersion) != (err == nil) {
			t.Fatalf("version %d: %v", version, err)
		}
	}
	claim.Intent.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE
	claim.Intent.OperationId = "media-delete-capability"
	claim.Intent.Patch = nil
	if _, err := RequestDigest(claim.Intent); err == nil {
		t.Fatal("PATCH capability accepted for DELETE")
	}
	claim.Intent.MaxOutcomeSchemaVersion = ServerErrorOutcomeVersion
	if _, err := RequestDigest(claim.Intent); err != nil {
		t.Fatalf("server error capability refused for DELETE: %v", err)
	}
}

func TestCompactPatchOutcomeRejectsTamperingEvenWithAValidSignature(t *testing.T) {
	tests := map[string]func(*mediav1.MediaPatchOutcome, *mediav1.MediaPatchClaim){
		"capability":             func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.Intent.MaxOutcomeSchemaVersion = 0 },
		"unsupported-capability": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.Intent.MaxOutcomeSchemaVersion = 5 },
		"delete": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			c.Intent.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE
		},
		"missing-patch":  func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.Intent.Patch = nil },
		"request":        func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.Intent.MediaId = "other" },
		"request-digest": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.RequestSha256 = nil },
		"before":         func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.BeforeSha256 = nil },
		"after":          func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.AfterSha256 = nil },
		"prefix":         func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.OperationId = "media-delete-compact" },
		"epoch":          func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.OriginalFence.ExecutionEpoch++ },
		"writer":         func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.WriterFencingToken++ },
		"fleet":          func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.FleetId = "other" },
		"environment":    func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Environment = "other" },
		"refused": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
		},
		"status": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.HttpStatus = 409 },
		"error":  func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.ErrorCode = "EFFECT_UNKNOWN" },
		"inline": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Media = &mediav1.MediaObject{} },
		"create": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.Created = &mediav1.MediaCreatedResult{}
		},
		"process": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.Processed = &mediav1.MediaProcessedResult{}
		},
		"deleted":         func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.DeletedCount = 1 },
		"missing-receipt": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Patched = nil },
		"record-digest":   func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Patched.RecordSha256 = nil },
		"zero-length":     func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Patched.RecordBytes = 0 },
		"other-length":    func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Patched.RecordBytes++ },
		"preparation":     func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Patched.PreparationSha256[0] ^= 1 },
		"original-fence": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.Patched.OriginalFence.ExecutionEpoch++
		},
		"unknown-outcome": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1})
		},
		"unknown-receipt": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.Patched.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1})
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			o, c, key := compactPatchFixture(t)
			mutate(o, c)
			signCompactPatch(t, o, key, compactPatchSignatureDomain)
			if VerifyOutcome(o, c, key.Public().(ed25519.PublicKey)) == nil {
				t.Fatal("tampered compact result accepted")
			}
		})
	}
	o, c, key := compactPatchFixture(t)
	for _, bad := range []*mediav1.MediaPatchClaim{nil, {}, {Intent: &mediav1.MediaPatchIntent{}}} {
		if VerifyOutcome(o, bad, key.Public().(ed25519.PublicKey)) == nil {
			t.Fatal("missing original claim accepted")
		}
	}
	o.Signature[0] ^= 1
	if VerifyOutcome(o, c, key.Public().(ed25519.PublicKey)) == nil {
		t.Fatal("wrong signature accepted")
	}
}
