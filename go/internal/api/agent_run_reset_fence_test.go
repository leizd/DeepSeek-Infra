package api

import (
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

// resetSharingFence decides whether a node's agent_reset is on the presented
// fence. Every successor mint goes through it, and its match rules were only
// half covered: the action-fence arm and the metadata-epoch arm are separate
// contracts.

func latestReset(t *testing.T, driver *agentRunDriver, run agent.RunMetadata, nodeID string) agent.RunMetadata {
	t.Helper()
	return retryingRun(t, driver, run, nodeID)
}

func TestResetSharingFenceAcceptsTheMetadataEpochArm(t *testing.T) {
	driver, _, run := newDriverFixture(t, false)
	updated := latestReset(t, driver, run, "coder")
	control := driver.service.control

	// No action/epoch pair matches, but the event carries the run id with the
	// metadata epoch, which the writer stamps. That arm must still admit.
	index, matched, err := resetSharingFence(control, updated.RunID, "coder", "", 0)
	if err != nil {
		t.Fatal(err)
	}
	if !matched {
		t.Fatal("the metadata-epoch arm did not match a real reset")
	}
	if index < 0 {
		t.Fatalf("matched reset reported index %d", index)
	}
}

func TestResetSharingFenceAcceptsTheActionFenceArm(t *testing.T) {
	driver, _, run := newDriverFixture(t, false)
	updated := latestReset(t, driver, run, "coder")
	control := driver.service.control

	record, present, err := control.Get("agent_run", updated.RunID)
	if err != nil || !present {
		t.Fatalf("run record: %v %v", present, err)
	}
	// The writer stamps actionId with the run id and executionEpoch with the
	// manifest epoch, so that exact pair is the action-fence arm.
	index, matched, err := resetSharingFence(control, updated.RunID, "coder", updated.RunID, record.ExecutionEpoch)
	if err != nil {
		t.Fatal(err)
	}
	if !matched || index < 0 {
		t.Fatalf("the action-fence arm did not match: %d %v %v", index, matched, err)
	}
}

func TestResetSharingFenceRejectsAnUnrelatedNodeAndAMissingHistory(t *testing.T) {
	driver, _, run := newDriverFixture(t, false)
	updated := latestReset(t, driver, run, "coder")
	control := driver.service.control

	// A reset for another node never authorizes this one.
	if _, matched, err := resetSharingFence(control, updated.RunID, "critic", updated.RunID, 1); err != nil || matched {
		t.Fatalf("another node matched: %v %v", matched, err)
	}
	// The metadata-epoch arm matches even when the caller passes a foreign action
	// id, because the writer's own run-id event is authoritative. Assert that
	// explicitly so the arm cannot silently narrow later.
	if _, matched, err := resetSharingFence(control, updated.RunID, "coder", "someone-else", 999); err != nil || !matched {
		t.Fatalf("the metadata arm stopped matching: %v %v", matched, err)
	}
	// A nil control cannot authorize anything and must not be read as a match.
	if index, matched, err := resetSharingFence(nil, updated.RunID, "coder", updated.RunID, 1); err != nil || matched || index != 0 {
		t.Fatalf("nil control: %d %v %v", index, matched, err)
	}
}
