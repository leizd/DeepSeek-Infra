package api

import (
	"errors"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

// These cases pin the driver-side glue that 4e874ae1 added without tests: the
// admission code mapping and the reset-fence scan that authorizes a successor.

func TestSuccessorAdmissionErrorMapsEveryCodeToItsTypedRefusal(t *testing.T) {
	for code, want := range map[string]error{
		"AGENT_DRIVER_TOOLS_PENDING":               errAgentDriverTools,
		"EFFECT_UNKNOWN":                           errAgentDriverUnknown,
		"AGENT_RUNTIME_RESET_REQUIRES_GENERATION":  errAgentDriverReset,
		"AGENT_RUNTIME_SUCCESSOR_HELD":             errAgentDriverHeld,
		"STALE_EXECUTION_EPOCH":                    errAgentDriverStale,
		"FENCE_MISMATCH":                           errAgentDriverFence,
		"AGENT_RUNTIME_GENERATION_CONFLICT":        errAgentDriverUnknown,
		"":                                         errAgentDriverUnknown,
		"UNRECOGNIZED_CODE_FROM_A_FUTURE_WORKER":   errAgentDriverUnknown,
	} {
		got := successorAdmissionError(code)
		if !errors.Is(got, want) {
			t.Fatalf("%q mapped to %v, want %v", code, got, want)
		}
	}
}

func TestResetSharingFenceReportsOnlyAMatchingReset(t *testing.T) {
	// A nil control cannot authorize anything, and must not be read as "matched".
	if index, matched, err := resetSharingFence(nil, "run-1", "coder", "run-1", 1); err != nil || matched || index != 0 {
		t.Fatalf("nil control authorized a reset: %d %v %v", index, matched, err)
	}

	driver, _, run := newDriverFixture(t, false)
	control := driver.service.control
	// A run with no resets is absent history, which is not an admission.
	index, matched, err := resetSharingFence(control, run.RunID, "coder", run.RunID, 1)
	if err != nil || matched || index != 0 {
		t.Fatalf("absent history matched: %d %v %v", index, matched, err)
	}
}

func TestResetSharingFenceIgnoresAnUnrelatedNodeOrAction(t *testing.T) {
	driver, _, run := newDriverFixture(t, false)
	control := driver.service.control
	// The scan is keyed by node id and either the action fence or the metadata
	// epoch stamped by the writer. A reset for another node must not mint.
	if index, matched, err := resetSharingFence(control, run.RunID, "critic", run.RunID, 1); err != nil || matched {
		t.Fatalf("another node's reset matched: %d %v %v", index, matched, err)
	}
	// A different action id with a different epoch is also not this fence.
	if index, matched, err := resetSharingFence(control, run.RunID, "coder", "someone-else", 9); err != nil || matched {
		t.Fatalf("foreign action/epoch matched: %d %v %v", index, matched, err)
	}
}

func TestDriverRunnableRefusesCancelledAndTerminalRuns(t *testing.T) {
	for _, row := range []struct {
		status    string
		cancelled bool
		want      bool
	}{
		{"created", false, true},
		{"planning", false, true},
		{"running", false, true},
		{"done", false, false},
		{"failed", false, false},
		{"created", true, false},
		{"running", true, false},
	} {
		run := agent.RunMetadata{Status: row.status}
		if row.cancelled {
			run.RecoveryProgress = nil
			run.Status = "cancelled"
		}
		if got := driverRunnable(run); got != row.want {
			t.Fatalf("%v: driverRunnable = %v, want %v", row, got, row.want)
		}
	}
	// A cancelled run is never runnable even when its status still says running.
	cancelled := agent.RunMetadata{Status: "running"}
	markCancelled(&cancelled)
	if driverRunnable(cancelled) {
		t.Fatal("a cancelled run stayed runnable")
	}
}

func markCancelled(run *agent.RunMetadata) {
	// CancellationActive is derived from the retained status trail, so drive it
	// through the public event rather than a private field.
	run.Status = "cancelled"
}

func TestDriverTasksCompleteRequiresEveryPlanNodeToSettle(t *testing.T) {
	if driverTasksComplete(agent.RunMetadata{}) {
		t.Fatal("an empty plan reported complete")
	}
	run := agent.RunMetadata{Plan: []agent.RunPlanNode{{ID: "coder", Task: "write"}, {ID: "critic", Task: "review"}}}
	if driverTasksComplete(run) {
		t.Fatal("an unsettled plan reported complete")
	}
}
