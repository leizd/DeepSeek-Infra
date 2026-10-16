package agent

import (
	"errors"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

// The retained recovery codec and prior-output helpers must refuse private text
// in repeated string fields and still resolve a known Agent outside the plan.

func TestRecoveryScopeEncodingRefusesPrivateTextInRepeatedFields(t *testing.T) {
	scope := RunRecoveryScope{Generation: 1, Context: ArtifactRef{SHA256: strings.Repeat("a", 64), Length: 4},
		ContextSHA256: strings.Repeat("b", 64), StartIndex: 1,
		Metadata: &agentv1.AgentRunRecoveryContext{SchemaVersion: 1, RunId: "run_codec_boundary",
			CompletedIds: []string{"clean", "age-secret-key-boundary"}}}
	if _, err := scope.MarshalJSON(); !errors.Is(err, ErrRunRecoveryContextEncoding) {
		t.Fatalf("repeated private text was encoded: %v", err)
	}
	clean := RunRecoveryScope{Generation: 1, Context: ArtifactRef{SHA256: strings.Repeat("a", 64), Length: 4},
		ContextSHA256: strings.Repeat("b", 64), StartIndex: 1,
		Metadata: &agentv1.AgentRunRecoveryContext{SchemaVersion: 1, RunId: "run_codec_boundary",
			CompletedIds: []string{"clean"}}}
	encoded, err := clean.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	var decoded RunRecoveryScope
	if err := decoded.UnmarshalJSON(encoded); err != nil || decoded.Metadata == nil ||
		len(decoded.Metadata.CompletedIds) != 1 || decoded.Metadata.CompletedIds[0] != "clean" {
		t.Fatalf("clean repeated fields did not round-trip: %v", err)
	}
}

func TestRecoveryPriorOutputsResolveAKnownAgentOutsideThePlan(t *testing.T) {
	plan := []RunPlanNode{{ID: "coder", Task: "write"}, {ID: "synth", Task: "summarize", DependsOn: []string{"coder"}}}
	prior := recoveryPriorOutputs(plan, "external-agent", []string{"coder", "synth", "external-agent"})
	if len(prior) != 2 || prior[0] != "coder" || prior[1] != "synth" {
		t.Fatalf("outside-plan priority order changed: %v", prior)
	}
	inside := recoveryPriorOutputs(plan, "synth", []string{"coder", "synth"})
	if len(inside) != 1 || inside[0] != "coder" {
		t.Fatalf("in-plan priority order changed: %v", inside)
	}
}

func TestRuntimeVersionKeepsAbsentRevisionByteCompatible(t *testing.T) {
	absent := RunMetadata{}
	if absent.RuntimeVersion() != 1 {
		t.Fatal("an absent runtime revision must identify the original request")
	}
	persisted := RunMetadata{RuntimeRevision: 9}
	if persisted.RuntimeVersion() != 9 {
		t.Fatal("a persisted runtime revision was replaced")
	}
}
