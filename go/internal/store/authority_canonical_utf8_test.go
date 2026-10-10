package store

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestAuthorityCanonicalJSONPreservesFrozenUTF8(t *testing.T) {
	value := map[string]any{"key": "对象<>&\u2028\u2029", "literal": "\\u2028", "epoch": json.Number("1")}
	raw, err := canonicalAuthorityJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	want := []byte("{\"epoch\":1,\"key\":\"对象<>&\u2028\u2029\",\"literal\":\"\\\\u2028\"}")
	if !bytes.Equal(raw, want) {
		t.Fatalf("frozen UTF-8 canonical mismatch: got %q, want %q", raw, want)
	}
	if _, err = canonicalAuthorityJSON(make(chan int)); err == nil {
		t.Fatal("accepted non-JSON signing material")
	}
	control, err := canonicalAuthorityJSON(map[string]any{"text": "\x00\n\t\"\\"})
	if err != nil || string(control) != "{\"text\":\"\\u0000\\n\\t\\\"\\\\\"}" {
		t.Fatalf("control escapes changed: %q %v", control, err)
	}
}
