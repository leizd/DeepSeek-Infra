package store

import (
	"encoding/json"
	"errors"
	"testing"

	"google.golang.org/protobuf/proto"
)

// A settlement field may only be installed by the dedicated settlement writer.
// The generic Put path must refuse it for every domain, not just the plan case
// that already had a test: a gap here lets any writer mint a signed marker and
// skip the verified settlement entirely.

func TestGenericWriterCannotInstallAnySettlementMarker(t *testing.T) {
	t.Run("synthesis", func(t *testing.T) {
		control, input, _ := synthesisSettlementFixture(t)
		before, _, err := control.Get("action", input.ActionID)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(before.Payload, &fields); err != nil {
			t.Fatal(err)
		}
		encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(input.Projection)
		if err != nil {
			t.Fatal(err)
		}
		fields[agentSynthesisSettlementField], _ = json.Marshal(encoded)
		forged := before
		forged.Revision++
		forged.State = "SUCCEEDED"
		forged.Payload, _ = json.Marshal(fields)
		if err := control.Put(forged); !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("generic writer installed a synthesis marker: %v", err)
		}
	})

	t.Run("cancellation", func(t *testing.T) {
		control, input := cancellationSettlementFixture(t, false)
		before, _, err := control.Get("action", input.ActionID)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(before.Payload, &fields); err != nil {
			t.Fatal(err)
		}
		fields[agentCancellationSettlementField], _ = json.Marshal([]byte("forged-marker"))
		forged := before
		forged.Revision++
		forged.State = "CANCELLED_CONFIRMED"
		forged.Payload, _ = json.Marshal(fields)
		if err := control.Put(forged); !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("generic writer installed a cancellation marker: %v", err)
		}
	})

	t.Run("failure", func(t *testing.T) {
		control, input, _ := failureSettlementFixture(t, "plan")
		before, _, err := control.Get("action", input.ActionID)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(before.Payload, &fields); err != nil {
			t.Fatal(err)
		}
		fields[agentFailureSettlementField], _ = json.Marshal([]byte("forged-marker"))
		forged := before
		forged.Revision++
		forged.State = "FAILED_BEFORE_EFFECT"
		forged.Payload, _ = json.Marshal(fields)
		if err := control.Put(forged); !errors.Is(err, ErrAgentRunExecutionRequired) {
			t.Fatalf("generic writer installed a failure marker: %v", err)
		}
	})
}

func TestGenericWriterCannotRemoveASettlementMarker(t *testing.T) {
	// The guard also has to cover removal: dropping the field would look like an
	// unsettled action and could re-open a settled effect for dispatch.
	control, input, _ := planSettlementFixture(t, false)
	settled, err := control.SettleAgentPlan(input)
	if err != nil {
		t.Fatalf("settle plan: %v", err)
	}
	record, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(record.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	if _, present := fields[agentPlanSettlementField]; !present {
		t.Fatal("the settled record carries no plan marker")
	}
	delete(fields, agentPlanSettlementField)
	forged := record
	forged.Revision++
	forged.Payload, _ = json.Marshal(fields)
	if err := control.Put(forged); !errors.Is(err, ErrAgentRunExecutionRequired) {
		t.Fatalf("generic writer removed a plan marker: %v", err)
	}
	_ = settled
}
