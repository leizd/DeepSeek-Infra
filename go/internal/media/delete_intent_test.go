package media

import (
	"bytes"
	"encoding/hex"
	"testing"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

func TestMediaDeleteIntentCannotCarryPatchOrUnknownKind(t *testing.T) {
	intent := &mediav1.MediaPatchIntent{
		SchemaVersion: 1, OperationId: "media-delete-fixture", MediaId: "media_one",
		Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE,
	}
	if _, err := RequestDigest(intent); err != nil {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*mediav1.MediaPatchIntent){
		"mixed-patch":  func(i *mediav1.MediaPatchIntent) { i.Patch = &mediav1.MediaPatch{} },
		"unknown-kind": func(i *mediav1.MediaPatchIntent) { i.Kind = mediav1.MediaMutationKind(2) },
		"patch-prefix": func(i *mediav1.MediaPatchIntent) { i.OperationId = "media-patch-fixture" },
		"empty-patch":  func(i *mediav1.MediaPatchIntent) { i.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED },
	} {
		t.Run(name, func(t *testing.T) {
			changed := proto.Clone(intent).(*mediav1.MediaPatchIntent)
			change(changed)
			if _, err := RequestDigest(changed); err == nil {
				t.Fatal("ambiguous media intent was accepted")
			}
		})
	}
}

func TestMediaLegacyPatchCanonicalBytesRetainDefaultKind(t *testing.T) {
	title, err := FromJSON("Legacy title")
	if err != nil {
		t.Fatal(err)
	}
	legacy := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-patch-legacy", MediaId: "media_one", Patch: &mediav1.MediaPatch{Title: title}}
	before, err := Encode(legacy)
	if err != nil {
		t.Fatal(err)
	}
	// Independently encoded from the frozen pre-DELETE descriptor, not the new
	// generated Go type. Existing stored claims must retain these exact bytes.
	want := "080112126d656469612d70617463682d6c65676163791a096d656469615f6f6e6522100a0e1a0c4c6567616379207469746c65"
	if hex.EncodeToString(before) != want {
		t.Fatal("the frozen pre-DELETE PATCH representation changed")
	}
	explicit := proto.Clone(legacy).(*mediav1.MediaPatchIntent)
	explicit.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED
	after, err := Encode(explicit)
	if err != nil || !bytes.Equal(before, after) {
		t.Fatal("the legacy PATCH canonical representation changed")
	}
	deleteIntent := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-delete-legacy", MediaId: "media_one", Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE}
	patchHash, err := RequestDigest(legacy)
	if err != nil || hex.EncodeToString(patchHash) != "e05fe127f5994d736cb5bf6c334808d5b95f9e39163b98682a4f0e8fc4b9d7c2" {
		t.Fatalf("frozen PATCH digest changed: %x/%v", patchHash, err)
	}
	deleteHash, err := RequestDigest(deleteIntent)
	if err != nil || bytes.Equal(patchHash, deleteHash) {
		t.Fatal("different media operations shared a request digest")
	}
}
