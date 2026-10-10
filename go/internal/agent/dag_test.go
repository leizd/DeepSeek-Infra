package agent

import "testing"

func TestEvaluateAdmitsNamedRuns(t *testing.T) {
	got := Evaluate(map[string]any{
		"agentRuns": []any{
			map[string]any{"runId": ""},
			map[string]any{"runId": "run-1"},
		},
	})
	rows := got["admissions"].([]any)
	if len(rows) != 2 {
		t.Fatalf("rows %d", len(rows))
	}
	if rows[0].(map[string]any)["decision"] != "REJECT" {
		t.Fatalf("empty %+v", rows[0])
	}
	if rows[1].(map[string]any)["decision"] != "ADMIT" {
		t.Fatalf("named %+v", rows[1])
	}
}
