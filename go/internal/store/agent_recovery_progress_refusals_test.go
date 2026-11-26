package store

import (
	"errors"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

// CommitAgentRecoveryProgress must refuse an incomplete expectation, a nil
// receipt, a fence that names another run, and a foreign run or stale revision.
// Those branches were uncovered; a wrong acceptance would commit recovery
// progress the controller never previewed.

func TestRecoveryProgressRefusesAnIncompleteExpectation(t *testing.T) {
	for name, mutate := range map[string]func(*CommitAgentRecoveryProgressRequest){
		"nil-receipt": func(in *CommitAgentRecoveryProgressRequest) { in.Receipt = nil },
		"empty-run":   func(in *CommitAgentRecoveryProgressRequest) { in.RunID = "" },
	} {
		t.Run(name, func(t *testing.T) {
			control, _, valid := progressGuardFixture(t, false)
			changed := valid
			mutate(&changed)
			if _, err := control.CommitAgentRecoveryProgress(changed); !errors.Is(err, ErrAgentRunMetadataInvalid) {
				t.Fatalf("%s was committed: %v", name, err)
			}
		})
	}
}

func TestRecoveryProgressRefusesAFenceForAnotherRun(t *testing.T) {
	control, _, valid := progressGuardFixture(t, false)
	foreign := valid
	foreign.AgentRecoveryProgressExpected.Fence = &internalprotocol.ActionFence{
		ActionId:       "run_that_is_not_this_one",
		ExecutionEpoch: valid.AgentRecoveryProgressExpected.Fence.ExecutionEpoch,
	}
	if _, err := control.CommitAgentRecoveryProgress(foreign); err == nil {
		t.Fatal("a fence for another run committed recovery progress")
	}
}

func TestRecoveryProgressRefusesAForeignOrStaleEnvelope(t *testing.T) {
	t.Run("foreign-run", func(t *testing.T) {
		control, _, valid := progressGuardFixture(t, false)
		foreign := valid
		foreign.AgentRecoveryProgressExpected.RunID = "run_that_does_not_own_this_action"
		if _, err := control.CommitAgentRecoveryProgress(foreign); err == nil {
			t.Fatal("a foreign run committed recovery progress")
		}
	})

	t.Run("stale-revision", func(t *testing.T) {
		control, _, valid := progressGuardFixture(t, false)
		stale := valid
		stale.AgentRecoveryProgressExpected.RuntimeRevision += 7
		if _, err := control.CommitAgentRecoveryProgress(stale); err == nil {
			t.Fatal("a stale revision committed recovery progress")
		}
	})

	t.Run("rebound-proof", func(t *testing.T) {
		control, _, valid := progressGuardFixture(t, false)
		// A receipt whose signature no longer covers its evidence cannot be
		// committed under this expectation.
		rebound := valid
		cloned := proto.Clone(valid.Receipt).(*agentv1.AgentRecoveryProgressReceipt)
		if len(cloned.Signature) > 0 {
			cloned.Signature[0] ^= 1
		} else {
			cloned.Signature = []byte{1}
		}
		rebound.Receipt = cloned
		if _, err := control.CommitAgentRecoveryProgress(rebound); err == nil {
			t.Fatal("a rebound proof was committed")
		}
	})
}
