package shadow

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestPythonGoDecisionDigestsMatchCanonicalCorpus(t *testing.T) {
	path := filepath.Join("..", "..", "..", "compat", "native-runtime", "v1", "control", "shadow_cases.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Cases []struct {
			Name     string         `json:"name"`
			Snapshot map[string]any `json:"snapshot"`
			Expect   struct {
				Digest string `json:"digest"`
			} `json:"expect"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if len(fixture.Cases) == 0 {
		t.Fatal("no shadow cases")
	}
	for _, item := range fixture.Cases {
		decision, err := Evaluate(item.Snapshot)
		if err != nil {
			t.Fatalf("%s: %v", item.Name, err)
		}
		got, _ := decision["digest"].(string)
		if got != item.Expect.Digest {
			t.Fatalf("%s goDecisionDigest=%s pythonDecisionDigest=%s", item.Name, got, item.Expect.Digest)
		}
		if denied, _ := decision["mutationDenied"].(bool); !denied {
			t.Fatalf("%s mutation was not denied", item.Name)
		}
	}
}
