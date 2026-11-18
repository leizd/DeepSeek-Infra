package store

import (
	"encoding/json"
	"testing"
)

func TestActionTransitionsRefuseForgedClaimsAndUnknownActions(t *testing.T) {
	control, source, _ := agentOutcomeFixture(t)
	lease, found, err := control.GetActionLease(source.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	payload := json.RawMessage(`{}`)
	if _, err := control.CompleteAction("action_missing_boundary", source.Epoch, lease.ClaimToken, payload); err == nil {
		t.Fatal("an unknown action completed")
	}
	if _, err := control.CompleteAction(source.ActionID, source.Epoch, "bogus-claim", payload); err == nil {
		t.Fatal("a forged claim completed an action")
	}
	if _, err := control.FailAction(source.ActionID, source.Epoch, "bogus-claim", payload); err == nil {
		t.Fatal("a forged claim failed an action")
	}
	if _, err := control.MarkActionEffectUnknown(source.ActionID, source.Epoch, "bogus-claim"); err == nil {
		t.Fatal("a forged claim marked an action effect unknown")
	}
}
