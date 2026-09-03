package shadow

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestReplayCanonicalFixturePersistsAndMatchesPythonDigests(t *testing.T) {
	control := openStore(t)
	path := filepath.Join("..", "..", "..", "compat", "native-runtime", "v1", "control", "shadow_cases.json")
	report, err := Replay(control, path)
	if err != nil {
		t.Fatal(err)
	}
	if report.Kernel != "control-shadow-decision-v1" || !report.MutationDenied || report.StoreDigest == "" || len(report.Cases) == 0 {
		t.Fatalf("report %+v", report)
	}
}

func TestReplayNilStoreStillChecksDigests(t *testing.T) {
	path := filepath.Join("..", "..", "..", "compat", "native-runtime", "v1", "control", "shadow_cases.json")
	report, err := Replay(nil, path)
	if err != nil {
		t.Fatal(err)
	}
	if report.StoreDigest != "" || len(report.Cases) == 0 {
		t.Fatalf("nil store %+v", report)
	}
}

func TestReplayDetectsDigestMismatch(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "bad.json")
	payload := map[string]any{
		"kernel": "control-shadow-decision-v1",
		"cases": []any{
			map[string]any{
				"name":     "bad",
				"snapshot": map[string]any{"actions": []any{}, "localFleetId": "fleet-a"},
				"expect":   map[string]any{"digest": "deadbeef"},
			},
		},
	}
	raw, _ := json.Marshal(payload)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Replay(nil, path); err == nil || err.Error() == "" {
		t.Fatal("mismatch")
	}
}

func TestReplayClosedStoreFails(t *testing.T) {
	control := openStore(t)
	_ = control.Close()
	path := filepath.Join("..", "..", "..", "compat", "native-runtime", "v1", "control", "shadow_cases.json")
	if _, err := Replay(control, path); err == nil {
		t.Fatal("closed")
	}
}

func TestReplayMissingFixtureFails(t *testing.T) {
	if _, err := Replay(nil, filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("missing")
	}
}

func TestReplayBadJSONFails(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bad.json")
	if err := os.WriteFile(path, []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Replay(nil, path); err == nil {
		t.Fatal("json")
	}
}
