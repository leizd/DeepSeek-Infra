package agent

import (
	"encoding/json"
	"testing"
)

func TestRecoveryCancellationUsesAnExplicitDurableGeneration(t *testing.T) {
	run := RunMetadata{BaseNodes: map[string]RunNode{}, Plan: []RunPlanNode{{ID: "coder", Task: "write"}}}
	run.ApplyRunEvent(RunEvent{Type: "run_status", Status: "cancelled", Index: 1})
	if !run.Cancelled || !run.CancellationActive() || run.Nodes()["coder"].State != "cancelled" {
		t.Fatal("legacy cancellation stopped being sticky")
	}
	run.Recovery = &RunRecoveryScope{Generation: 1}
	run.ApplyRunEvent(RunEvent{Type: "run_status", Status: "running", Index: 2})
	if !run.Cancelled || run.CancellationActive() || run.Nodes()["coder"].State != "queued" {
		t.Fatal("a new scope erased old cancellation or inherited it as current")
	}
	run.ApplyRunEvent(RunEvent{Type: "run_status", Status: "cancelled", Index: 3})
	if !run.Cancelled || !run.CancellationActive() || run.CancellationGeneration != 1 {
		t.Fatal("new generation cancellation was not retained")
	}
	run.ApplyRunEvent(RunEvent{Type: "run_status", Status: "running", Index: 4})
	if !run.CancellationActive() || run.Nodes()["coder"].State != "cancelled" {
		t.Fatal("a status append bypassed generation admission")
	}
	encoded, err := json.Marshal(run)
	if err != nil {
		t.Fatal(err)
	}
	var restored RunMetadata
	if err := json.Unmarshal(encoded, &restored); err != nil || !restored.CancellationActive() || !restored.Cancelled {
		t.Fatal("cancellation generation did not survive persistence", err)
	}
	restored.Recovery.Generation = 2
	if restored.CancellationActive() || !restored.Cancelled {
		t.Fatal("next scope lost the retained cancellation history")
	}
}
