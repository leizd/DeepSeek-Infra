package agent

import (
	"testing"
)

func FuzzAgentEvaluate(f *testing.F) {
	f.Add("run-1")
	f.Add("")
	f.Add("   ")
	f.Add("run-with-special-chars-@#$!")

	f.Fuzz(func(t *testing.T, runID string) {
		snapshot := map[string]any{
			"agentRuns": []any{
				map[string]any{
					"runId": runID,
				},
			},
		}

		res := Evaluate(snapshot)
		if res == nil {
			t.Fatal("Evaluate returned nil")
		}
		admissions, ok := res["admissions"].([]any)
		if !ok || len(admissions) != 1 {
			t.Fatalf("expected 1 admission, got %d", len(admissions))
		}
		adm, ok := admissions[0].(map[string]any)
		if !ok {
			t.Fatal("admission row not map")
		}
		dec, _ := adm["decision"].(string)
		reason, _ := adm["reason"].(string)

		if runID == "" {
			if dec != "REJECT" || reason != "EMPTY_RUN_ID" {
				t.Fatalf("empty run ID should be rejected, got %s / %s", dec, reason)
			}
		} else {
			if dec != "ADMIT" || reason != "OK" {
				t.Fatalf("named run ID should be admitted, got %s / %s", dec, reason)
			}
		}
	})
}
