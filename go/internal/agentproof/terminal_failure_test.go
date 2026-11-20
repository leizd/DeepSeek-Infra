package agentproof

import (
	"crypto/ed25519"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func TestTerminalFailureOutcomeRequiresConclusivePhaseSpecificEvidence(t *testing.T) {
	for _, phase := range []agentv1.AgentRunExecutionPhase{agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS} {
		for _, status := range []uint32{400, 401, 402, 422, 429} {
			effect, intent, binding, private := signedNodeFixture(t)
			e := effect.OutcomeReceipt.Evidence
			intent.Phase, e.Phase = phase, phase
			if phase == agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS {
				intent.NodeId, e.NodeId = "synthesizer", "synthesizer"
			}
			e.ProviderPlan = nil
			e.Failure = &agentv1.AgentProviderFailureMetadata{SchemaVersion: 1, HttpStatus: status, ResponseSha256: strings.Repeat("f", 64)}
			resignNodeFixture(t, effect.OutcomeReceipt, private)
			if _, err := VerifyAgentNodeOutcome(effect.OutcomeReceipt, intent, 7, binding); err != nil {
				t.Fatalf("proved rejection %v/%d refused: %v", phase, status, err)
			}
			for name, mutate := range map[string]func(*agentv1.AgentNodeOutcomeEvidence){
				"server-error": func(e *agentv1.AgentNodeOutcomeEvidence) { e.Failure.HttpStatus = 503 },
				"redirect":     func(e *agentv1.AgentNodeOutcomeEvidence) { e.Failure.HttpStatus = 307 },
				"not-found":    func(e *agentv1.AgentNodeOutcomeEvidence) { e.Failure.HttpStatus = 404 },
				"success":      func(e *agentv1.AgentNodeOutcomeEvidence) { e.Failure.HttpStatus = 200 },
				"schema":       func(e *agentv1.AgentNodeOutcomeEvidence) { e.Failure.SchemaVersion = 2 },
				"response":     func(e *agentv1.AgentNodeOutcomeEvidence) { e.Failure.ResponseSha256 = "unconfirmed" },
				"unknown":      func(e *agentv1.AgentNodeOutcomeEvidence) { e.Failure.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
				"plan": func(e *agentv1.AgentNodeOutcomeEvidence) {
					e.ProviderPlan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "invented"}}
				},
				"counters": func(e *agentv1.AgentNodeOutcomeEvidence) {
					e.TaskOutput = &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, Failed: true}
				},
			} {
				t.Run(name, func(t *testing.T) {
					bad := proto.Clone(effect.OutcomeReceipt).(*agentv1.AgentNodeOutcomeReceipt)
					mutate(bad.Evidence)
					resignNodeFixture(t, bad, private)
					if _, err := VerifyAgentNodeOutcome(bad, intent, 7, binding); err == nil {
						t.Fatal("signed malformed rejection became terminal evidence")
					}
				})
			}
			intent.Phase, e.Phase, intent.NodeId, e.NodeId = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS, "coder", "coder"
			resignNodeFixture(t, effect.OutcomeReceipt, private)
			if _, err := VerifyAgentNodeOutcome(effect.OutcomeReceipt, intent, 7, binding); err == nil {
				t.Fatal("ordinary failed task authorized terminal run failure")
			}
		}
	}
}

func TestFailureProjectionBindsSourceErrorBodyPhaseAndSettlementFence(t *testing.T) {
	_, _, binding, private := signedNodeFixture(t)
	expected := &agentv1.AgentFailureProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: strings.Repeat("a", 64),
		SettlementFence: &commonv1.ActionFence{ActionId: "failed-plan", ExecutionEpoch: 2}, SettlementWriterToken: 3,
		RunId: "run", Phase: agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, MetadataEpoch: 1, ExpectedIndex: 0,
		ErrorBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 92}, FleetId: binding.FleetID, Environment: binding.Environment}
	receipt := &agentv1.AgentFailureProjectionReceipt{SchemaVersion: 1, Evidence: proto.Clone(expected).(*agentv1.AgentFailureProjectionEvidence), SignerPublicKey: binding.PublicKey}
	sign := func(r *agentv1.AgentFailureProjectionReceipt, domain string) {
		raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(r.Evidence)
		if err != nil {
			t.Fatal(err)
		}
		r.Signature = ed25519.Sign(private, append([]byte(domain), raw...))
	}
	sign(receipt, FailureProjectionDomain)
	if err := VerifyAgentFailureProjection(receipt, expected, binding); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*agentv1.AgentFailureProjectionEvidence){
		"source":      func(e *agentv1.AgentFailureProjectionEvidence) { e.SourceReceiptSha256 = strings.Repeat("c", 64) },
		"body":        func(e *agentv1.AgentFailureProjectionEvidence) { e.ErrorBody.Sha256 = strings.Repeat("c", 64) },
		"body-length": func(e *agentv1.AgentFailureProjectionEvidence) { e.ErrorBody.Length++ },
		"body-unknown": func(e *agentv1.AgentFailureProjectionEvidence) {
			e.ErrorBody.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"action": func(e *agentv1.AgentFailureProjectionEvidence) { e.SettlementFence.ActionId = "other" },
		"epoch":  func(e *agentv1.AgentFailureProjectionEvidence) { e.SettlementFence.ExecutionEpoch++ },
		"writer": func(e *agentv1.AgentFailureProjectionEvidence) { e.SettlementWriterToken++ },
		"phase": func(e *agentv1.AgentFailureProjectionEvidence) {
			e.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS
		},
		"task-phase": func(e *agentv1.AgentFailureProjectionEvidence) {
			e.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS
		},
		"run":            func(e *agentv1.AgentFailureProjectionEvidence) { e.RunId = "other" },
		"cursor":         func(e *agentv1.AgentFailureProjectionEvidence) { e.ExpectedIndex++ },
		"metadata-epoch": func(e *agentv1.AgentFailureProjectionEvidence) { e.MetadataEpoch++ },
		"fleet":          func(e *agentv1.AgentFailureProjectionEvidence) { e.FleetId = "other" },
		"environment":    func(e *agentv1.AgentFailureProjectionEvidence) { e.Environment = "other" },
	} {
		t.Run(name, func(t *testing.T) {
			bad := proto.Clone(receipt).(*agentv1.AgentFailureProjectionReceipt)
			mutate(bad.Evidence)
			sign(bad, FailureProjectionDomain)
			if VerifyAgentFailureProjection(bad, expected, binding) == nil {
				t.Fatal("rebound signed failure projection accepted")
			}
		})
	}
	for _, domain := range []string{NodeOutcomeDomain, PlanProjectionDomain, SynthesisProjectionDomain, ""} {
		sign(receipt, domain)
		if VerifyAgentFailureProjection(receipt, expected, binding) == nil {
			t.Fatal("different proof purpose authorized failure settlement")
		}
	}
}
