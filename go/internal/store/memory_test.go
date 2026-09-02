package store

import "testing"

func TestOpenRejectsProductionPath(t *testing.T) {
	if _, err := Open("/var/lib/deepseek"); err == nil {
		t.Fatal("production path must be denied")
	}
	store, err := Open("")
	if err != nil {
		t.Fatal(err)
	}
	if err := store.MutateProduction("policy", map[string]any{"id": "p1"}); err == nil {
		t.Fatal("production mutate must be denied")
	}
	if err := store.PutShadow("decision", map[string]any{"digest": "abc"}); err != nil {
		t.Fatal(err)
	}
	value, ok := store.GetShadow("decision")
	if !ok || value["digest"] != "abc" {
		t.Fatalf("shadow get: %v %v", value, ok)
	}
}
