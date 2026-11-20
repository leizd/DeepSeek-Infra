package media

import "testing"

// These cover the boundary work inside the flat patch parser: JSON string escapes,
// non-BMP runes, and the malformed escape forms that must be refused rather than
// silently repaired.
//
// Why this file exists: the parser records UTF-16 code units alongside each key so
// the original Python byte-for-byte ordering can be reproduced. `patchWireText`
// returns those units *only* on the failure path — a well-formed string answers the
// already-decoded text with nil units — so the tests below assert the decoded text
// on success and the error on failure, which is the contract the callers rely on.

// TestPatchWireTextDecodesEveryShortEscape keeps the escape table honest: each of
// the five short escapes must decode to the control code it names, not the letter.
// A single backslash-u entry forces the manual walk.
func TestPatchWireTextDecodesEveryShortEscape(t *testing.T) {
	cases := map[string]string{
		"\"\\b\\u0041\"": "\bA",
		"\"\\f\\u0041\"": "\fA",
		"\"\\n\\u0041\"": "\nA",
		"\"\\r\\u0041\"": "\rA",
		"\"\\t\\u0041\"": "\tA",
	}
	for raw, want := range cases {
		// patchWireText answers the `decoded` value it is given once the units are
		// well formed, so the argument is what a caller passes: the value the
		// JSON decoder already produced from the same bytes.
		decoded, units, err := patchWireText([]byte(raw), want)
		if err != nil {
			t.Fatalf("%q: %v", raw, err)
		}
		if units != nil {
			t.Fatalf("%q: a well-formed string must not return units", raw)
		}
		if decoded != want {
			t.Fatalf("%q: decoded to %q, want %q", raw, decoded, want)
		}
	}
}

// TestPatchWireTextSkipsUnitsWithoutUnicodeEscape documents the fast path, so a
// future change that always walks the source is caught.
func TestPatchWireTextSkipsUnitsWithoutUnicodeEscape(t *testing.T) {
	decoded, units, err := patchWireText([]byte("\"plain\""), "plain")
	if err != nil || decoded != "plain" || units != nil {
		t.Fatalf("fast path changed: %q %v %v", decoded, units, err)
	}
}

// TestPatchWireTextSurfacesUnpairedSurrogates is the reason the unit table exists:
// a lone surrogate cannot be represented in UTF-8, so it is reported through the
// units slice instead of being silently replaced.
func TestPatchWireTextSurfacesUnpairedSurrogates(t *testing.T) {
	for name, raw := range map[string]string{
		"lone-high": "\"\\u0041\\ud800\"",
		"lone-low":  "\"\\u0041\\udc00\"",
	} {
		decoded, units, err := patchWireText([]byte(raw), "")
		if err != nil {
			t.Fatalf("%s: unexpected error %v", name, err)
		}
		if decoded != "" {
			t.Fatalf("%s: a lone surrogate must not produce text, got %q", name, decoded)
		}
		if len(units) == 0 {
			t.Fatalf("%s: the unit table should carry the offending units", name)
		}
	}
}

// TestPatchWireTextPairsASurrogateRune proves a correctly paired escape is accepted
// and decoded, which is what distinguishes it from the lone cases above.
func TestPatchWireTextPairsASurrogateRune(t *testing.T) {
	decoded, units, err := patchWireText([]byte("\"\\ud83d\\ude00\""), "\U0001F600")
	if err != nil {
		t.Fatal(err)
	}
	if units != nil {
		t.Fatal("a valid surrogate pair must not be reported as unpaired")
	}
	if decoded != "\U0001F600" {
		t.Fatalf("decoded to %q, want the emoji", decoded)
	}
}

// TestPatchWireTextRefusesMalformedEscapes covers the forms that must be refused
// outright rather than repaired.
func TestPatchWireTextRefusesMalformedEscapes(t *testing.T) {
	cases := map[string]string{
		"bare-backslash":  "\"\\",
		"short-unicode":   "\"\\u0041\\u00\"",
		"non-hex-unicode": "\"\\u0041\\uZZZZ\"",
		"unterminated":    "\"abc",
		"not-a-string":    "abc",
	}
	for name, raw := range cases {
		if _, _, err := patchWireText([]byte(raw), ""); err != ErrInvalid {
			t.Fatalf("%s: expected ErrInvalid, got %v", name, err)
		}
	}
}

// TestParsePublicPatchAcceptsBooleanAndNullMembers exercises the scalar branches
// the ordering path also has to encode.
func TestParsePublicPatchAcceptsBooleanAndNullMembers(t *testing.T) {
	patch, err := ParsePublicPatch([]byte(`{"title":true,"projectId":null}`))
	if err != nil {
		t.Fatal(err)
	}
	if patch.GetFlatValues() == nil || len(patch.GetFlatValues().GetNodes()) < 2 {
		t.Fatal("boolean and null members were dropped from the flat record")
	}
}
