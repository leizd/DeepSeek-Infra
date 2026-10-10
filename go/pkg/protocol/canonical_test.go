package protocol

import (
	"encoding/json"
	"testing"
)

func TestCanonicalHelpers(t *testing.T) {
	if AsString("x") != "x" || AsBool(true) != true || AsInt(3.0) != 3 || AsInt(4) != 4 {
		t.Fatal("scalars")
	}
	if AsInt(int64(5)) != 5 || AsInt(json.Number("6")) != 6 {
		t.Fatal("int64")
	}
	got, ok := AsFloat(1.5)
	if !ok || got != 1.5 {
		t.Fatal("float")
	}
	if len(AsMap(nil)) != 0 || AsMap(map[string]any{"a": 1})["a"] != 1 {
		t.Fatal("map")
	}
	if len(AsList(nil)) != 0 || len(AsList([]any{1})) != 1 {
		t.Fatal("list")
	}
	digest, err := Digest(map[string]any{"k": "v"})
	if err != nil || digest == "" {
		t.Fatalf("digest %q %v", digest, err)
	}
	if AsInt("x") != 0 {
		t.Fatal("bad int")
	}
	if _, ok := AsFloat(json.Number("2.5")); !ok {
		t.Fatal("number float")
	}
	if _, ok := AsFloat(int64(2)); !ok {
		t.Fatal("int64 float")
	}
	if _, ok := AsFloat(3); !ok {
		t.Fatal("int float")
	}
	if _, ok := AsFloat("no"); ok {
		t.Fatal("bad float")
	}
	if _, err := CanonicalJSON(map[string]any{"a": 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := Digest(make(chan int)); err == nil {
		t.Fatal("digest must fail")
	}
}
