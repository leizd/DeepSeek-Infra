package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"strings"
	"testing"

	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

func serverErrorFixture(t *testing.T, delete bool, applied bool) (*mediav1.MediaPatchOutcome, *mediav1.MediaPatchClaim, ed25519.PrivateKey) {
	t.Helper()
	o, c, key := compactPatchFixture(t)
	c.Intent.MaxOutcomeSchemaVersion = ServerErrorOutcomeVersion
	if delete {
		c.Intent.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE
		c.Intent.OperationId = "media-delete-server-error"
		c.Intent.Patch = nil
	}
	c.Fence.ActionId = c.Intent.OperationId
	request, err := RequestDigest(c.Intent)
	if err != nil {
		t.Fatal(err)
	}
	c.RequestSha256 = request
	o.SchemaVersion, o.OperationId = ServerErrorOutcomeVersion, c.Intent.OperationId
	o.OriginalFence = proto.Clone(c.Fence).(*commonv1.ActionFence)
	o.RequestSha256, o.HttpStatus = request, 500
	o.ErrorCode, o.ErrorMessage, o.Patched = "internal", "Server error", nil
	o.State, o.AfterSha256 = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED, nil
	if applied {
		after := sha256.Sum256([]byte("the original publication completed before response encoding failed"))
		o.State, o.AfterSha256 = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, after[:]
	}
	signCompactPatch(t, o, key, serverErrorSignatureDomain)
	return o, c, key
}

func TestMediaServerErrorOutcomeKeepsAppliedAndRefusedEffectsDistinct(t *testing.T) {
	for _, test := range []struct {
		name    string
		delete  bool
		applied bool
	}{{"patch-applied", false, true}, {"patch-refused", false, false}, {"delete-refused", true, false}} {
		t.Run(test.name, func(t *testing.T) {
			for _, capability := range []uint32{ServerErrorOutcomeVersion, FlatPatchOutcomeVersion} {
				o, c, key := serverErrorFixture(t, test.delete, test.applied)
				c.Intent.MaxOutcomeSchemaVersion = capability
				request, err := RequestDigest(c.Intent)
				if err != nil {
					t.Fatal(err)
				}
				o.RequestSha256, c.RequestSha256 = request, request
				signCompactPatch(t, o, key, serverErrorSignatureDomain)
				for _, state := range []string{"CLAIMED", "DISPATCHED", "EFFECT_UNKNOWN", "APPLIED", "REFUSED"} {
					c.State = state
					if err := VerifyOutcome(o, c, key.Public().(ed25519.PublicKey)); err != nil {
						t.Fatalf("historical %s capability %d: %v", state, capability, err)
					}
				}
			}
		})
	}
}

func TestMediaServerErrorOutcomeRejectsReboundOrAmbiguousSignedEffects(t *testing.T) {
	changes := map[string]func(*mediav1.MediaPatchOutcome, *mediav1.MediaPatchClaim){
		"capability":      func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.Intent.MaxOutcomeSchemaVersion = 0 },
		"claim-version":   func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.SchemaVersion++ },
		"missing-fence":   func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.Fence = nil },
		"request":         func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.Intent.MediaId = "media_other" },
		"invalid-request": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { c.Intent.Patch = nil },
		"prefix": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.OperationId, c.Intent.OperationId, c.Fence.ActionId, o.OriginalFence.ActionId = "unbound", "unbound", "unbound", "unbound"
		},
		"epoch":   func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.OriginalFence.ExecutionEpoch++ },
		"realm":   func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Environment = "other" },
		"writer":  func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.WriterFencingToken++ },
		"status":  func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.HttpStatus = 200 },
		"message": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.ErrorMessage = "different" },
		"unknown-state": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_UNKNOWN
		},
		"unchanged-applied": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.AfterSha256 = bytes.Clone(o.BeforeSha256)
		},
		"missing-after": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.AfterSha256 = nil },
		"refused-with-after": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
		},
		"delete-applied": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			c.Intent.Kind, c.Intent.Patch = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE, nil
			o.OperationId, c.Intent.OperationId, c.Fence.ActionId, o.OriginalFence.ActionId = "media-delete-error", "media-delete-error", "media-delete-error", "media-delete-error"
		},
		"inline-result": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) { o.Media = &mediav1.MediaObject{} },
		"unknown-outcome": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.ProtoReflect().SetUnknown([]byte{0xf8, 7, 1})
		},
		"unknown-fence": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			c.Fence.ProtoReflect().SetUnknown([]byte{0xf8, 7, 1})
		},
		"oversized-envelope": func(o *mediav1.MediaPatchOutcome, c *mediav1.MediaPatchClaim) {
			o.FleetId = strings.Repeat("f", 2*1024*1024)
			c.FleetId = o.FleetId
		},
	}
	for name, change := range changes {
		t.Run(name, func(t *testing.T) {
			o, c, key := serverErrorFixture(t, false, true)
			change(o, c)
			signCompactPatch(t, o, key, serverErrorSignatureDomain)
			if err := VerifyOutcome(o, c, key.Public().(ed25519.PublicKey)); err != ErrInvalid {
				t.Fatal("signed ambiguous server error was accepted", err)
			}
		})
	}
}

func TestMediaServerErrorOutcomeRequiresOriginalClaimEncodingAndSigningDomain(t *testing.T) {
	o, c, key := serverErrorFixture(t, false, true)
	public := key.Public().(ed25519.PublicKey)
	for _, claim := range []*mediav1.MediaPatchClaim{nil, {}, {Intent: &mediav1.MediaPatchIntent{}}} {
		if VerifyOutcome(o, claim, public) != ErrInvalid {
			t.Fatal("missing original claim accepted")
		}
	}
	if VerifyOutcome(o, c, public[:len(public)-1]) != ErrInvalid {
		t.Fatal("short signer key accepted")
	}
	for _, domain := range [][]byte{signatureDomain, compactPatchSignatureDomain, flatPatchSignatureDomain} {
		signCompactPatch(t, o, key, domain)
		if VerifyOutcome(o, c, public) != ErrInvalid {
			t.Fatal("another schema's signing domain accepted")
		}
	}
	signCompactPatch(t, o, key, serverErrorSignatureDomain)
	o.FleetId, c.FleetId = string([]byte{0xff}), string([]byte{0xff})
	if VerifyOutcome(o, c, public) != ErrInvalid {
		t.Fatal("invalid UTF-8 signed envelope accepted")
	}
}
