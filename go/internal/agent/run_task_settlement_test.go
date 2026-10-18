package agent

import "testing"

func TestTaskOutputSettledRequiresConsistentSignedProjectionState(t *testing.T) {
	for _, state := range []string{"", "queued", "running", "retrying", "blocked", "cancelled", "succeeded", "failed", "EFFECT_UNKNOWN"} {
		for _, failed := range []bool{false, true} {
			node := RunNode{ID: "coder", State: state, Failed: failed}
			expected := state == "succeeded" && !failed || state == "failed" && failed
			if node.TaskOutputSettled() != expected {
				t.Fatalf("state=%q failed=%v settled=%v", state, failed, node.TaskOutputSettled())
			}
		}
	}
}
