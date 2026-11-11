package media

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
)

func flatInputField(t *testing.T, record *mediav1.MediaFlatPatchRecord, node *mediav1.MediaFlatPatchNode, key string) *mediav1.MediaFlatPatchNode {
	t.Helper()
	object := node.GetObjectValue()
	if object != nil {
		for _, member := range object.Members {
			if member.Key == key && len(member.KeyUtf16Le) == 0 {
				return record.Nodes[member.Child-1]
			}
		}
	}
	t.Fatalf("missing object member %q", key)
	return nil
}

func flatInputIntent(patch *mediav1.MediaPatch) *mediav1.MediaPatchIntent {
	return &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-patch-input-proof",
		MediaId: "media_input", Patch: patch, MaxOutcomeSchemaVersion: FlatPatchOutcomeVersion}
}

func TestFlatPatchInputRetainsEscapesAndScalarKinds(t *testing.T) {
	patch, err := ParsePublicPatch([]byte(`{"metadata":[null,true,false,"😀\u0061\b\f\n\r\t\"\/\\"]}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := RequestDigest(flatInputIntent(patch)); err != nil {
		t.Fatal("legal scalar array could not be admitted", err)
	}
	actual, err := flatPatchJSON(patch.FlatValues)
	text, _ := json.Marshal("😀a\b\f\n\r\t\"/\\")
	want := `{"metadata":[null,true,false,` + string(text) + `]}`
	if err != nil || string(actual) != want {
		t.Fatalf("scalar kinds or escapes changed: %s, %v", actual, err)
	}
}

func TestFlatPatchSyntaxRetainsDiagnosticsAfterEmptyContainersAndDecimals(t *testing.T) {
	// Original public wrapper plus the observed Python 3.12 JSONDecodeError.
	const expected = "Invalid JSON: Expecting property name enclosed in double quotes: line 1 column 28 (char 27)"
	for _, raw := range []string{`{"metadata":{},"title":1.5,}`, `{"metadata":[],"title":1e2,}`} {
		patch, err := ParsePublicPatch([]byte(raw))
		if patch != nil || err == nil || err.Error() != expected {
			t.Fatalf("malformed request diagnostic changed: %q, %v", raw, err)
		}
	}
}

func TestFlatPatchInputPreservesDeepDecimalsNonfiniteAndUTF16(t *testing.T) {
	const wide = "1234567890123456789012345678901234567890"
	deep := strings.Repeat("{\"next\":", 160) + wide + strings.Repeat("}", 160)
	raw := []byte("{\"metadata\":{\"deep\":" + deep +
		",\"values\":[NaN,Infinity,-Infinity,1e400,\"NaN\",\"\\ud800\",\"\\ud83d\\ude00\",\"\\\\ud800\"]},\"title\":null}")
	patch, err := ParsePublicPatch(raw)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := RequestDigest(flatInputIntent(patch)); err != nil {
		t.Fatal("typed input was rejected", err)
	}
	record := patch.FlatValues
	root := record.Nodes[len(record.Nodes)-1]
	if !flatInputField(t, record, root, "title").GetNullValue() || patch.Title != nil {
		t.Fatal("explicit null was lost or mixed with nested representation")
	}
	metadata := flatInputField(t, record, root, "metadata")
	value := flatInputField(t, record, metadata, "deep")
	for i := 0; i < 160; i++ {
		value = flatInputField(t, record, value, "next")
	}
	if value.GetNumberValue() != wide {
		t.Fatal("exact integer changed")
	}
	array := flatInputField(t, record, metadata, "values").GetArrayValue().Children
	for i, number := range []string{"NaN", "Infinity", "-Infinity", "1e400"} {
		if record.Nodes[array[i]-1].GetNumberValue() != number {
			t.Fatalf("number %q was changed", number)
		}
	}
	if record.Nodes[array[4]-1].GetTextValue() != "NaN" ||
		!bytes.Equal(record.Nodes[array[5]-1].GetTextUtf16Le(), []byte{0x00, 0xd8}) ||
		record.Nodes[array[6]-1].GetTextValue() != "😀" ||
		record.Nodes[array[7]-1].GetTextValue() != "\\ud800" {
		t.Fatal("retained strings lost their distinction")
	}
	if _, err := flatPatchJSON(record); err != ErrInvalid {
		t.Fatal("an input-only value reached strict public response encoding", err)
	}
}

func TestFlatPatchInputPrunesOverwrittenAndUnsupportedValues(t *testing.T) {
	raw := []byte("{\"metadata\":{\"discard\":NaN},\"ignored\":{\"bad\":\"\\ud800\"}," +
		"\"metadata\":{\"a?\":\"first\",\"a!\":\"second\",\"a?\":\"last\",\"\\ud800\":\"one\",\"\\uD800\":\"two\"}}")
	patch, err := ParsePublicPatch(raw)
	if err != nil {
		t.Fatal(err)
	}
	intent := flatInputIntent(patch)
	if _, err := RequestDigest(intent); err != nil {
		t.Fatal(err)
	}
	record := patch.FlatValues
	if len(record.Nodes) != 5 {
		t.Fatalf("unexpected reachable nodes %d", len(record.Nodes))
	}
	root := record.Nodes[len(record.Nodes)-1]
	members := flatInputField(t, record, root, "metadata").GetObjectValue().Members
	if len(members) != 3 || members[0].Key != "a?" || members[1].Key != "a!" ||
		!bytes.Equal(members[2].KeyUtf16Le, []byte{0x00, 0xd8}) ||
		record.Nodes[members[0].Child-1].GetTextValue() != "last" ||
		record.Nodes[members[2].Child-1].GetTextValue() != "two" {
		t.Fatal("first position and last duplicate value were not retained")
	}
	intent.MaxOutcomeSchemaVersion = ServerErrorOutcomeVersion
	if _, err := RequestDigest(intent); err != ErrInvalid {
		t.Fatal("legacy capability accepted flat input", err)
	}
	intent.MaxOutcomeSchemaVersion = FlatPatchOutcomeVersion
	patch.Title = &mediav1.MediaValue{Kind: &mediav1.MediaValue_TextValue{TextValue: "mixed"}}
	if _, err := RequestDigest(intent); err != ErrInvalid {
		t.Fatal("mixed input representations were accepted", err)
	}
}

func TestFlatPatchInputKeepsTheOriginalOneMegabyteBound(t *testing.T) {
	const prefix = "{\"metadata\":{\"value\":["
	const suffix = "]}}"
	const limit = 1_000_000
	count := (limit - len(prefix) - len(suffix) + 1) / 2
	raw := prefix + strings.Repeat("0,", count-1) + "0" + suffix
	raw += strings.Repeat(" ", limit-len(raw))
	patch, err := ParsePublicPatch([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := RequestDigest(flatInputIntent(patch)); err != nil {
		t.Fatal("legal HTTP request exceeded the retained Protobuf bound", err)
	}
	if _, err := ParsePublicPatch([]byte(raw + " ")); err != ErrInvalid {
		t.Fatal("over-limit input was admitted", err)
	}
}

func TestFlatPatchInputRejectsInvalidBodyBeforeAdmittingAnOperation(t *testing.T) {
	for _, test := range []struct {
		raw  string
		want error
	}{
		{"{\"metadata\":{\"value\":\"\xff\"}}", ErrPatchUTF8},
		{"[NaN]", ErrPatchObject},
		{"{\"ignored\":NaN}", ErrPatchFields},
	} {
		if patch, err := ParsePublicPatch([]byte(test.raw)); patch != nil || err != test.want {
			t.Fatalf("invalid body result %v, %v", patch, err)
		}
	}
}

func TestFlatPatchSyntaxMatchesOriginalDiagnostics(t *testing.T) {
	raw, err := os.ReadFile("../../../tests/fixtures/native_media_patch_json_errors_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		CanonicalPythonVersion string `json:"canonicalPythonVersion"`
		Cases                  []struct {
			Name     string `json:"name"`
			Request  []byte `json:"requestBase64"`
			BodyJSON string `json:"bodyJson"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.CanonicalPythonVersion != "3.12.12" || len(fixture.Cases) != 36 {
		t.Fatal("unexpected original diagnostics corpus")
	}
	for _, sample := range fixture.Cases {
		t.Run(sample.Name, func(t *testing.T) {
			var body struct {
				Error string `json:"error"`
				Code  string `json:"code"`
			}
			if err := json.Unmarshal([]byte(sample.BodyJSON), &body); err != nil {
				t.Fatal(err)
			}
			patch, err := ParsePublicPatch(sample.Request)
			syntax, classified := err.(*PatchJSONSyntaxError)
			if patch != nil || !classified || syntax.Error() != body.Error || body.Code != "invalid_payload" {
				t.Fatalf("diagnostic %v, want %q", err, body.Error)
			}
		})
	}
}

func TestFlatPatchKeepsOrderInLegacyResponseValues(t *testing.T) {
	const original = `{"metadata":{"existing":"kept","a":[{"z":null,"a":1234567890123456789012345678901234567890}]},"title":"changed"}`
	ordered, err := ParseJSON([]byte(original))
	if err != nil {
		t.Fatal(err)
	}
	record := ordered.GetObjectValue()
	encoded, err := PatchResponseJSON(record)
	if err != nil || string(encoded) != original {
		t.Fatalf("ordered response %s, %v", encoded, err)
	}
	record.Members[0].SourceOrdinal = record.Members[1].SourceOrdinal
	if _, err := PatchResponseJSON(record); err != ErrInvalid {
		t.Fatal("duplicate ordinal was rendered", err)
	}
	unordered, err := FromJSON(map[string]any{"z": "last", "a": "first"})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err = PatchResponseJSON(unordered.GetObjectValue())
	if err != nil || string(encoded) != `{"a":"first","z":"last"}` {
		t.Fatalf("legacy response %s, %v", encoded, err)
	}
}
