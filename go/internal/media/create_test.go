package media

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
	"testing"
)

func TestMediaCreateManifestBindsContentWithoutExpandingControlRows(t *testing.T) {
	raw, err := json.Marshal(map[string]any{"type": "pdf", "metadata": map[string]any{"long": string(bytes.Repeat([]byte{'x'}, 1_500_000))}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = ParseJSON(raw); err == nil {
		t.Fatal("PATCH bound unexpectedly widened")
	}
	value, err := ParseJSONBounded(raw, 16_000_000)
	if err != nil {
		t.Fatal("original legal creation rejected", err)
	}
	payload, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&mediav1.MediaCreatePayload{Registration: value.GetObjectValue()})
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(payload)
	intent := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-create-fixture", MediaId: "media_one", Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_CREATE,
		Create: &mediav1.MediaCreateManifest{PayloadSha256: digest[:], PayloadBytes: uint64(len(payload))}}
	if _, err = RequestDigest(intent); err != nil {
		t.Fatal(err)
	}
	encoded, err := Encode(intent)
	if err != nil || len(encoded) > 512 {
		t.Fatal("content leaked into historical control row", len(encoded), err)
	}
	for name, change := range map[string]func(*mediav1.MediaPatchIntent){
		"missing-manifest": func(i *mediav1.MediaPatchIntent) { i.Create = nil },
		"mixed-patch":      func(i *mediav1.MediaPatchIntent) { i.Patch = &mediav1.MediaPatch{} },
		"mixed-process":    func(i *mediav1.MediaPatchIntent) { i.Process = &mediav1.MediaProcessOptions{Force: true} },
		"missing-sha":      func(i *mediav1.MediaPatchIntent) { i.Create.PayloadSha256 = nil },
		"empty-length":     func(i *mediav1.MediaPatchIntent) { i.Create.PayloadBytes = 0 },
		"excess-length":    func(i *mediav1.MediaPatchIntent) { i.Create.PayloadBytes = MaxCreatePayloadBytes + 1 },
		"patch-kind":       func(i *mediav1.MediaPatchIntent) { i.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_UNSPECIFIED },
		"delete-kind":      func(i *mediav1.MediaPatchIntent) { i.Kind = mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_DELETE },
	} {
		t.Run(name, func(t *testing.T) {
			changed := proto.Clone(intent).(*mediav1.MediaPatchIntent)
			change(changed)
			if _, err := RequestDigest(changed); err == nil {
				t.Fatal("ambiguous creation accepted")
			}
		})
	}
}
