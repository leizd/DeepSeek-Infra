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

func TestFlatPatchJSONRejectsDamagedAndNoncanonicalWireRecords(t *testing.T) {
	record := &mediav1.MediaFlatPatchRecord{Nodes: []*mediav1.MediaFlatPatchNode{
		{Kind: &mediav1.MediaFlatPatchNode_BoolValue{BoolValue: true}},
		flatObject(&mediav1.MediaFlatPatchMember{Key: "enabled", Child: 1}),
	}}
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	if actual, err := FlatPatchJSON(encoded); string(actual) != `{"enabled":true}` || err != nil {
		t.Fatalf("canonical record returned %q, %v", actual, err)
	}
	// A non-minimal encoding of the same field tag decodes to the same record.
	noncanonical := append([]byte{encoded[0] | 0x80, 0}, encoded[1:]...)
	for _, wire := range [][]byte{encoded[:len(encoded)-1], noncanonical} {
		if actual, err := FlatPatchJSON(wire); actual != nil || err != ErrInvalid {
			t.Fatalf("bad wire record returned %q, %v", actual, err)
		}
	}
}

func TestFlatPatchValidatorRejectsInvalidTypedValuesAndInputFields(t *testing.T) {
	for _, tc := range []struct {
		name   string
		input  bool
		change func(*mediav1.MediaFlatPatchRecord)
	}{
		{"false null", false, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[0].Kind = &mediav1.MediaFlatPatchNode_NullValue{}
		}},
		{"invalid UTF-8 value", false, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[0].Kind = &mediav1.MediaFlatPatchNode_TextValue{TextValue: string([]byte{0xff})}
		}},
		{"truncated UTF-16 value", true, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[0].Kind = &mediav1.MediaFlatPatchNode_TextUtf16Le{TextUtf16Le: []byte{0xd8}}
		}},
		{"missing nested object", false, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[0].Kind = &mediav1.MediaFlatPatchNode_ObjectValue{}
		}},
		{"missing nested array", false, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[1].Kind = &mediav1.MediaFlatPatchNode_ArrayValue{}
		}},
		{"invalid array reference", false, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[1].GetArrayValue().Children[0] = 2
		}},
		{"ambiguous object key", true, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[2].GetObjectValue().Members[0].KeyUtf16Le = []byte{0, 0xd8}
		}},
		{"empty input", true, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes = []*mediav1.MediaFlatPatchNode{flatObject()}
		}},
		{"unsupported input field", true, func(r *mediav1.MediaFlatPatchRecord) {
			r.Nodes[2].GetObjectValue().Members[0].Key = "ignored"
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			record := &mediav1.MediaFlatPatchRecord{Nodes: []*mediav1.MediaFlatPatchNode{
				{Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: "text"}},
				{Kind: &mediav1.MediaFlatPatchNode_ArrayValue{
					ArrayValue: &mediav1.MediaFlatPatchArray{Children: []uint32{1}},
				}},
				flatObject(&mediav1.MediaFlatPatchMember{Key: "metadata", Child: 2}),
			}}
			if err := validateFlatRecord(record, tc.input); err != nil {
				t.Fatalf("fixture is invalid: %v", err)
			}
			tc.change(record)
			if err := validateFlatRecord(record, tc.input); err != ErrInvalid {
				t.Fatalf("invalid value returned %v", err)
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
