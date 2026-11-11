package media

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"strings"
	"testing"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

func flatObject(members ...*mediav1.MediaFlatPatchMember) *mediav1.MediaFlatPatchNode {
	return &mediav1.MediaFlatPatchNode{Kind: &mediav1.MediaFlatPatchNode_ObjectValue{
		ObjectValue: &mediav1.MediaFlatPatchObject{Members: members},
	}}
}

func TestFlatPatchWireRejectsNoncanonicalEncodingAndInvalidScalarNodes(t *testing.T) {
	empty := &mediav1.MediaFlatPatchRecord{Nodes: []*mediav1.MediaFlatPatchNode{flatObject()}}
	canonical, err := (proto.MarshalOptions{Deterministic: true}).Marshal(empty)
	if err != nil || len(canonical) < 2 || canonical[1] >= 128 {
		t.Fatal("invalid fixture", err)
	}
	// Both protobufs decode, but the overlong length has a different byte identity.
	overlong := append([]byte{canonical[0], canonical[1] | 0x80, 0}, canonical[2:]...)
	for _, raw := range [][]byte{{0x0a, 0x80}, overlong} {
		if result, err := FlatPatchJSON(raw); result != nil || err != ErrInvalid {
			t.Fatal("malformed or noncanonical wire returned public JSON", err)
		}
	}
	unknownArray := &mediav1.MediaFlatPatchArray{}
	unknownArray.ProtoReflect().SetUnknown([]byte{0x78, 1})
	unknownObject := &mediav1.MediaFlatPatchObject{}
	unknownObject.ProtoReflect().SetUnknown([]byte{0x78, 1})
	for name, node := range map[string]*mediav1.MediaFlatPatchNode{
		"false-null":                    {Kind: &mediav1.MediaFlatPatchNode_NullValue{NullValue: false}},
		"invalid-utf8":                  {Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: string([]byte{0xff})}},
		"nil-array":                     {Kind: &mediav1.MediaFlatPatchNode_ArrayValue{}},
		"unknown-array":                 {Kind: &mediav1.MediaFlatPatchNode_ArrayValue{ArrayValue: unknownArray}},
		"array-cycle":                   {Kind: &mediav1.MediaFlatPatchNode_ArrayValue{ArrayValue: &mediav1.MediaFlatPatchArray{Children: []uint32{1}}}},
		"nil-object":                    {Kind: &mediav1.MediaFlatPatchNode_ObjectValue{}},
		"unknown-object":                {Kind: &mediav1.MediaFlatPatchNode_ObjectValue{ObjectValue: unknownObject}},
		"odd-utf16":                     {Kind: &mediav1.MediaFlatPatchNode_TextUtf16Le{TextUtf16Le: []byte{0, 0xd8, 0}}},
		"canonical-utf16-must-use-text": {Kind: &mediav1.MediaFlatPatchNode_TextUtf16Le{TextUtf16Le: []byte{'a', 0}}},
	} {
		t.Run(name, func(t *testing.T) {
			record := &mediav1.MediaFlatPatchRecord{Nodes: []*mediav1.MediaFlatPatchNode{node, flatObject(&mediav1.MediaFlatPatchMember{Key: "metadata", Child: 1})}}
			if _, err := RequestDigest(flatInputIntent(&mediav1.MediaPatch{FlatValues: record})); err != ErrInvalid {
				t.Fatal("malformed typed input was admitted", err)
			}
			if result, err := flatPatchJSON(record); result != nil || err != ErrInvalid {
				t.Fatal("malformed typed result returned JSON", err)
			}
		})
	}
	for _, record := range []*mediav1.MediaFlatPatchRecord{
		empty,
		{Nodes: []*mediav1.MediaFlatPatchNode{flatObject(), flatObject(&mediav1.MediaFlatPatchMember{Key: "unsupported", Child: 1})}},
		{Nodes: []*mediav1.MediaFlatPatchNode{flatObject(), flatObject(&mediav1.MediaFlatPatchMember{KeyUtf16Le: []byte{0, 0xd8}, Child: 1})}},
	} {
		if _, err := RequestDigest(flatInputIntent(&mediav1.MediaPatch{FlatValues: record})); err != ErrInvalid {
			t.Fatal("unsupported public input key was admitted", err)
		}
	}
}

func TestFlatPatchPreservesDeepObjectsOrderAndExactNumbers(t *testing.T) {
	const decimal = "1234567890123456789012345678901234567890"
	const text = "中文<&\n"
	nodes := []*mediav1.MediaFlatPatchNode{
		{Kind: &mediav1.MediaFlatPatchNode_NumberValue{NumberValue: decimal}},
		{Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: text}},
		{Kind: &mediav1.MediaFlatPatchNode_BoolValue{BoolValue: false}},
		{Kind: &mediav1.MediaFlatPatchNode_NullValue{NullValue: true}},
	}
	deep := uint32(1)
	for i := 0; i < 160; i++ {
		nodes = append(nodes, flatObject(&mediav1.MediaFlatPatchMember{Key: "next", Child: deep}))
		deep = uint32(len(nodes))
	}
	nodes = append(nodes, &mediav1.MediaFlatPatchNode{Kind: &mediav1.MediaFlatPatchNode_ArrayValue{
		ArrayValue: &mediav1.MediaFlatPatchArray{Children: []uint32{2, 3, 4}},
	}})
	nodes = append(nodes, flatObject(
		&mediav1.MediaFlatPatchMember{Key: "z", Child: deep},
		&mediav1.MediaFlatPatchMember{Key: "a", Child: uint32(len(nodes))},
	))
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&mediav1.MediaFlatPatchRecord{Nodes: nodes})
	if err != nil {
		t.Fatal(err)
	}
	actual, err := FlatPatchJSON(encoded)
	if err != nil {
		t.Fatal(err)
	}
	quoted, _ := json.Marshal(text)
	expected := "{\"z\":" + strings.Repeat("{\"next\":", 160) + decimal +
		strings.Repeat("}", 160) + ",\"a\":[" + string(quoted) + ",false,null]}"
	if string(actual) != expected {
		t.Fatal("flat result changed original nesting, order or scalar values")
	}
}

func TestFlatPatchRejectsInvalidTypedTreesBeforeReturningJSON(t *testing.T) {
	fixture := &mediav1.MediaFlatPatchRecord{Nodes: []*mediav1.MediaFlatPatchNode{
		{Kind: &mediav1.MediaFlatPatchNode_NumberValue{NumberValue: "1"}},
		{Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: "text"}},
		flatObject(&mediav1.MediaFlatPatchMember{Key: "a", Child: 1}, &mediav1.MediaFlatPatchMember{Key: "b", Child: 2}),
	}}
	tests := map[string]func(*mediav1.MediaFlatPatchRecord){
		"zero":   func(r *mediav1.MediaFlatPatchRecord) { r.Nodes[2].GetObjectValue().Members[0].Child = 0 },
		"self":   func(r *mediav1.MediaFlatPatchRecord) { r.Nodes[2].GetObjectValue().Members[0].Child = 3 },
		"shared": func(r *mediav1.MediaFlatPatchRecord) { r.Nodes[2].GetObjectValue().Members[1].Child = 1 },
		"orphan": func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[2].GetObjectValue().Members = r.Nodes[2].GetObjectValue().Members[:1]
		},
		"key":     func(r *mediav1.MediaFlatPatchRecord) { r.Nodes[2].GetObjectValue().Members[1].Key = "a" },
		"missing": func(r *mediav1.MediaFlatPatchRecord) { r.Nodes[1].Kind = nil },
		"number": func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[0].Kind = &mediav1.MediaFlatPatchNode_NumberValue{NumberValue: "1 2"}
		},
		"root": func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[2].Kind = &mediav1.MediaFlatPatchNode_BoolValue{BoolValue: true}
		},
		"unknown": func(r *mediav1.MediaFlatPatchRecord) { r.Nodes[1].ProtoReflect().SetUnknown([]byte{0x78, 1}) },
	}
	for name, change := range tests {
		t.Run(name, func(t *testing.T) {
			record := proto.Clone(fixture).(*mediav1.MediaFlatPatchRecord)
			change(record)
			encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(record)
			if err != nil {
				t.Fatal(err)
			}
			if output, err := FlatPatchJSON(encoded); err != ErrInvalid || output != nil {
				t.Fatal("invalid tree returned JSON", err)
			}
		})
	}
}

func TestFlatPatchBindsCapabilityPreparationCodecAndSigningDomain(t *testing.T) {
	for _, test := range []struct {
		name                string
		capability, binding uint32
		domain              []byte
		want                bool
	}{
		{"original-flat", FlatPatchOutcomeVersion, 2, flatPatchSignatureDomain, true},
		{"legacy-capability", ServerErrorOutcomeVersion, 2, flatPatchSignatureDomain, false},
		{"legacy-preparation", FlatPatchOutcomeVersion, 1, flatPatchSignatureDomain, false},
		{"legacy-signing-domain", FlatPatchOutcomeVersion, 2, compactPatchSignatureDomain, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			outcome, claim, key := compactPatchFixture(t)
			claim.Intent.MaxOutcomeSchemaVersion = test.capability
			digest, err := RequestDigest(claim.Intent)
			if err != nil {
				t.Fatal(err)
			}
			claim.RequestSha256 = digest
			outcome.RequestSha256 = digest
			outcome.SchemaVersion = FlatPatchOutcomeVersion
			original := proto.Clone(claim).(*mediav1.MediaPatchClaim)
			original.State = "CLAIMED"
			preparation, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&mediav1.MediaPatchPreparation{
				SchemaVersion: test.binding, Claim: original, RecordSha256: outcome.Patched.RecordSha256,
				RecordBytes: outcome.Patched.RecordBytes,
			})
			if err != nil {
				t.Fatal(err)
			}
			binding := sha256.Sum256(preparation)
			outcome.Patched.PreparationSha256 = binding[:]
			outcome.Signature = nil
			signCompactPatch(t, outcome, key, test.domain)
			if err := VerifyOutcome(outcome, claim, key.Public().(ed25519.PublicKey)); (err == nil) != test.want {
				t.Fatal("flat receipt verification differs", err)
			}
		})
	}
}
