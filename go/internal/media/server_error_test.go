package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"testing"

	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

// A server-error outcome is the one signed result whose whole payload is a fixed
// refusal: it exists so an operator can distinguish "the write was refused with a
// 500" from "the write's effect is unknown". These tests pin that the builder only
// accepts the exact refusal shape and that every mutable-looking field is covered
// by the signature.
//
// The keys here are ephemeral and cannot grant an admission or an effect; there is
// no production private key in this package.

type serverErrorFixture struct {
	public  ed25519.PublicKey
	private ed25519.PrivateKey
	claim   *mediav1.MediaPatchClaim
	outcome *mediav1.MediaPatchOutcome
}

func newServerErrorFixture(t *testing.T, kind mediav1.MediaMutationKind) serverErrorFixture {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	operationID := "media-patch-server-error"
	if kind == mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE {
		operationID = "media-delete-server-error"
	}
	// RequestDigest refuses a PATCH intent without a Patch body, and the
	// server-error schema version is only legal for the delete kind. The
	// unspecified (patch) kind therefore carries a flat-record patch, which is the
	// shape RequestDigest accepts at FlatPatchOutcomeVersion.
	intent := &mediav1.MediaPatchIntent{
		SchemaVersion:           1,
		OperationId:             operationID,
		MediaId:                 "media_one",
		Kind:                    kind,
		MaxOutcomeSchemaVersion: FlatPatchOutcomeVersion,
	}
	if kind != mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE {
		intent.OperationId = "media-patch-server-error"
		// A flat record is a node table whose last node is the root object and
		// whose members reference earlier nodes; validateFlatRecord enforces that
		// shape, so the text node must exist before the object that references it.
		intent.Patch = &mediav1.MediaPatch{
			FlatValues: &mediav1.MediaFlatPatchRecord{Nodes: []*mediav1.MediaFlatPatchNode{
				{Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: "retained"}},
				flatObject(&mediav1.MediaFlatPatchMember{Key: "title", Child: 1}),
			}},
		}
	}
	digest, err := RequestDigest(intent)
	if err != nil {
		t.Fatal(err)
	}
	fence := &commonv1.ActionFence{ActionId: operationID, ExecutionEpoch: 1}
	claim := &mediav1.MediaPatchClaim{
		SchemaVersion:      1,
		Intent:             intent,
		Fence:              fence,
		RequestSha256:      digest,
		Owner:              "writer",
		ClaimToken:         "token",
		WriterFencingToken: 7,
		FleetId:            "fixture-fleet",
		Environment:        "test",
		LeaseUntilUnix:     1,
	}
	outcome := &mediav1.MediaPatchOutcome{
		SchemaVersion:      1,
		OperationId:        operationID,
		OriginalFence:      fence,
		RequestSha256:      digest,
		State:              mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED,
		HttpStatus:         500,
		ErrorCode:          "internal",
		ErrorMessage:       "Server error",
		BeforeSha256:       bytes.Repeat([]byte{1}, 32),
		FleetId:            claim.FleetId,
		Environment:        claim.Environment,
		WriterFencingToken: claim.WriterFencingToken,
	}
	return serverErrorFixture{public: public, private: private, claim: claim, outcome: outcome}
}

func (fixture serverErrorFixture) sign(t *testing.T, outcome *mediav1.MediaPatchOutcome) {
	t.Helper()
	outcome.Signature = nil
	encoded, err := Encode(outcome)
	if err != nil {
		t.Fatal(err)
	}
	message := append(append([]byte(nil), serverErrorSignatureDomain...), encoded...)
	outcome.Signature = ed25519.Sign(fixture.private, message)
}

// TestServerErrorNegotiationAcceptsOnlyRefusalShapes covers the negotiation
// predicate, which decides whether an outcome may even be read as a server error.
func TestServerErrorNegotiationAcceptsOnlyRefusalShapes(t *testing.T) {
	for _, version := range []uint32{ServerErrorOutcomeVersion, FlatPatchOutcomeVersion} {
		for _, kind := range []mediav1.MediaMutationKind{
			mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED,
			mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE,
		} {
			intent := &mediav1.MediaPatchIntent{MaxOutcomeSchemaVersion: version, Kind: kind}
			if !serverErrorNegotiated(intent) {
				t.Fatalf("version %d kind %v should negotiate a server error", version, kind)
			}
		}
	}
	for name, intent := range map[string]*mediav1.MediaPatchIntent{
		"nil":         nil,
		"old-version": {MaxOutcomeSchemaVersion: 1},
		"create-kind": {MaxOutcomeSchemaVersion: ServerErrorOutcomeVersion, Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_CREATE},
		"no-version":  {Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE},
	} {
		if serverErrorNegotiated(intent) {
			t.Fatalf("%s should not negotiate a server error", name)
		}
	}
}

// TestServerErrorOutcomeRoundTripsPerMutationKind is the acceptance path: a
// correctly signed refusal is accepted for both patch and delete intents, which is
// also what pins the operation-id prefix rule.
func TestServerErrorOutcomeRoundTripsPerMutationKind(t *testing.T) {
	for _, kind := range []mediav1.MediaMutationKind{
		mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED,
		mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE,
	} {
		fixture := newServerErrorFixture(t, kind)
		fixture.sign(t, fixture.outcome)
		if err := verifyServerErrorOutcome(fixture.outcome, fixture.claim, fixture.public); err != nil {
			t.Fatalf("kind %v: a valid refusal was refused: %v", kind, err)
		}
	}
}

// TestServerErrorOutcomeRefusesEachDivergence walks the individual guards. Each
// case is a field the signature covers, so accepting it would let a signed refusal
// be rebound to a different request.
func TestServerErrorOutcomeRefusesEachDivergence(t *testing.T) {
	divergences := map[string]func(*mediav1.MediaPatchOutcome, *mediav1.MediaPatchClaim){
		"operation-id-mismatch": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.OperationId = "media-patch-other"
			o.OriginalFence = &commonv1.ActionFence{ActionId: "media-patch-other", ExecutionEpoch: 1}
		},
		"wrong-prefix": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.OperationId = "unrelated-server-error"
		},
		"status-not-500":      func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) { o.HttpStatus = 503 },
		"wrong-error-code":    func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) { o.ErrorCode = "conflict" },
		"wrong-error-message": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) { o.ErrorMessage = "something else" },
		"carried-media": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.Media = &mediav1.MediaObject{}
		},
		"carried-deleted-count": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.DeletedCount = 1
		},
		"short-before-digest": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.BeforeSha256 = []byte{1}
		},
		"zero-fencing-token": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.WriterFencingToken = 0
		},
		"fleet-mismatch":       func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) { o.FleetId = "other-fleet" },
		"environment-mismatch": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) { o.Environment = "production" },
		"applied-without-after": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED
		},
		"applied-with-empty-after": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED
			o.AfterSha256 = o.BeforeSha256
		},
		"refused-with-after": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.AfterSha256 = bytes.Repeat([]byte{2}, 32)
		},
		"other-state": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_UNSPECIFIED
		},
		"fence-epoch": func(o *mediav1.MediaPatchOutcome, _ *mediav1.MediaPatchClaim) {
			o.OriginalFence = &commonv1.ActionFence{ActionId: o.OperationId, ExecutionEpoch: 2}
		},
	}
	for name, mutate := range divergences {
		t.Run(name, func(t *testing.T) {
			fixture := newServerErrorFixture(t, mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED)
			mutate(fixture.outcome, fixture.claim)
			fixture.sign(t, fixture.outcome)
			if err := verifyServerErrorOutcome(fixture.outcome, fixture.claim, fixture.public); err == nil {
				t.Fatalf("%s was accepted", name)
			}
		})
	}
}

// TestServerErrorOutcomeRefusesAStaleSignature proves the signature is what
// actually gates the outcome: mutating a covered field without re-signing is
// refused, so the verify path is not merely a shape check.
func TestServerErrorOutcomeRefusesAStaleSignature(t *testing.T) {
	fixture := newServerErrorFixture(t, mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED)
	fixture.sign(t, fixture.outcome)
	fixture.outcome.HttpStatus = 503
	if err := verifyServerErrorOutcome(fixture.outcome, fixture.claim, fixture.public); err == nil {
		t.Fatal("a mutated but unre-signed refusal was accepted")
	}
	fixture.outcome.HttpStatus = 500
	fixture.outcome.Signature = bytes.Repeat([]byte{0}, ed25519.SignatureSize)
	if err := verifyServerErrorOutcome(fixture.outcome, fixture.claim, fixture.public); err == nil {
		t.Fatal("a forged signature was accepted")
	}
}

// TestServerErrorOutcomeRefusesMissingOrWrongClaimInputs covers the argument
// guards that run before any field comparison.
func TestServerErrorOutcomeRefusesMissingOrWrongClaimInputs(t *testing.T) {
	fixture := newServerErrorFixture(t, mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED)
	fixture.sign(t, fixture.outcome)

	cases := map[string]struct {
		claim  *mediav1.MediaPatchClaim
		public []byte
	}{
		"nil-claim": {nil, fixture.public},
		"nil-fence": {&mediav1.MediaPatchClaim{Intent: fixture.claim.Intent}, fixture.public},
		"wrong-claim-version": {func() *mediav1.MediaPatchClaim {
			clone := proto.Clone(fixture.claim).(*mediav1.MediaPatchClaim)
			clone.SchemaVersion = 2
			return clone
		}(), fixture.public},
		"short-public-key": {
			func() *mediav1.MediaPatchClaim {
				return proto.Clone(fixture.claim).(*mediav1.MediaPatchClaim)
			}(),
			[]byte{1, 2, 3},
		},
		"short-request-digest": {func() *mediav1.MediaPatchClaim {
			clone := proto.Clone(fixture.claim).(*mediav1.MediaPatchClaim)
			clone.RequestSha256 = []byte{1}
			return clone
		}(), fixture.public},
		"empty-fleet": {func() *mediav1.MediaPatchClaim {
			clone := proto.Clone(fixture.claim).(*mediav1.MediaPatchClaim)
			clone.FleetId = ""
			return clone
		}(), fixture.public},
		"empty-environment": {func() *mediav1.MediaPatchClaim {
			clone := proto.Clone(fixture.claim).(*mediav1.MediaPatchClaim)
			clone.Environment = ""
			return clone
		}(), fixture.public},
	}
	for name, testCase := range cases {
		t.Run(name, func(t *testing.T) {
			if err := verifyServerErrorOutcome(fixture.outcome, testCase.claim, testCase.public); err == nil {
				t.Fatalf("%s was accepted", name)
			}
		})
	}
}
