package api

import (
	"context"
	"reflect"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func TestDriverRecoveryQueryRefusesMissingOrMalformedOriginalReceipt(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	before := driverFixtureRun(t, driver, run.RunID)
	if driverQuery(nil) != nil {
		t.Fatal("missing custody invented a reconciliation query")
	}
	for _, receipt := range []*agentv1.AgentNodeOutcomeReceipt{nil, {SignerPublicKey: "\xff"}} {
		if _, err := driverReceiptDigest(receipt); err == nil {
			t.Fatal("missing or malformed original receipt acquired a digest")
		}
		if driverQuery(&agentv1.AgentNodeEffectMetadata{OperationId: "original-operation", OutcomeReceipt: receipt}) != nil {
			t.Fatal("unverified bytes became a new custody query")
		}
	}
	if !reflect.DeepEqual(before, driverFixtureRun(t, driver, run.RunID)) || len(fixture.calls) != 0 {
		t.Fatal("unverified query changed the run or executed a provider effect")
	}
	// A signed offline fixture checks Go query binding; it cannot qualify Rust
	// custody or a provider. Querying it must not invoke the fixture again.
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	var original *agentv1.AgentNodeEffectMetadata
	for _, effect := range fixture.effects {
		if effect.NodeId == "leader" {
			original = effect
		}
	}
	if original == nil || len(fixture.calls) != 1 {
		t.Fatal("offline source custody was not retained")
	}
	query := driverQuery(original)
	if query == nil || !reflect.DeepEqual(query.Fence, original.Fence) ||
		query.OperationId != original.OperationId || query.RunId != original.RunId || query.NodeId != original.NodeId || len(query.ExpectedReceiptSha256) != 64 {
		t.Fatal("reconciliation lost the original request identity")
	}
	if len(fixture.calls) != 1 {
		t.Fatal("custody lookup caused a second dispatch")
	}
}

func TestDriverSynthesisOnlyRecoveryCannotReadmitTaskExecution(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	run.Status = "running"
	run.Recovery = &agent.RunRecoveryScope{Generation: 1, Metadata: &agentv1.AgentRunRecoveryContext{Synthesize: true}}
	phase, existing, err := driver.phase(run)
	if err != nil || existing != nil || phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS {
		t.Fatal("synthesis-only recovery selected task dispatch", phase, err)
	}
	if len(fixture.calls) != 0 {
		t.Fatal("phase selection issued a provider effect")
	}
	actions, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(actions) != 0 {
		t.Fatal("phase selection admitted an unrequested action", err)
	}
}
