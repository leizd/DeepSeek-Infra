package agent

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// Expectations come from the unchanged original Python pure functions,
// captured offline without importing a runtime or opening any user store.
func TestRunMetadataMatchesOriginalReferenceAtEveryEvent(t *testing.T) {
	var corpus struct {
		SchemaVersion uint32 `json:"schemaVersion"`
		Cases         []struct {
			Name        string     `json:"name"`
			Events      []RunEvent `json:"events"`
			Checkpoints []struct {
				Status     string             `json:"status"`
				Plan       []RunPlanNode      `json:"plan"`
				Nodes      map[string]RunNode `json:"nodes"`
				FinalAfter int64              `json:"finalAfter"`
			} `json:"checkpoints"`
		} `json:"cases"`
	}
	raw, err := os.ReadFile("testdata/agent_run_metadata_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatal(err)
	}
	if corpus.SchemaVersion != 1 || len(corpus.Cases) != 62 {
		t.Fatal("incomplete reference corpus")
	}
	for _, test := range corpus.Cases {
		t.Run(test.Name, func(t *testing.T) {
			run := RunMetadata{Status: "created", Plan: []RunPlanNode{}, FinalAfter: -1}
			if len(test.Events) != len(test.Checkpoints) {
				t.Fatal("missing checkpoints")
			}
			for index, event := range test.Events {
				event.Index = int64(index)
				if event.Type == "agent_plan" {
					event.Plan = NormalizeRunPlan(event.Plan)
				}
				run.ApplyRunEvent(event)
				want := test.Checkpoints[index]
				if run.Status != want.Status || !reflect.DeepEqual(run.Plan, want.Plan) || !reflect.DeepEqual(run.Nodes(), want.Nodes) ||
					run.FinalAfter != want.FinalAfter || run.NextIndex != int64(index+1) {
					actual, _ := json.Marshal(map[string]any{"status": run.Status, "plan": run.Plan, "nodes": run.Nodes(), "finalAfter": run.FinalAfter})
					expected, _ := json.Marshal(want)
					t.Fatalf("event %d (%s): actual %s; reference %s", index, event.Type, actual, expected)
				}
			}
		})
	}
}
