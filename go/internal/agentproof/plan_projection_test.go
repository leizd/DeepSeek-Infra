package agentproof

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func TestPlanProjectionRequiresIndependentScopeSignatureAndCanonicalFields(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	binding := Binding{PublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "fleet", Environment: "test"}
	expected := &agentv1.AgentPlanProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: strings.Repeat("a", 64), SettlementFence: &commonv1.ActionFence{ActionId: "planner", ExecutionEpoch: 2},
		SettlementWriterToken: 3, RunId: "run", MetadataEpoch: 1, ExpectedIndex: 2, NormalizedPlan: []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}}, NextStatus: "running", FleetId: binding.FleetID, Environment: binding.Environment}
	receipt := &agentv1.AgentPlanProjectionReceipt{SchemaVersion: 1, SignerPublicKey: binding.PublicKey, Evidence: proto.Clone(expected).(*agentv1.AgentPlanProjectionEvidence)}
	receipt.Evidence.PlanBody = &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 10}
	receipt.Evidence.StatusBody = &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 20}
	sign := func(r *agentv1.AgentPlanProjectionReceipt) {
		raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(r.Evidence)
		if err != nil {
			t.Fatal(err)
		}
		r.Signature = ed25519.Sign(private, append([]byte(PlanProjectionDomain), raw...))
	}
	sign(receipt)
	if err := VerifyAgentPlanProjection(receipt, expected, binding); err != nil {
		t.Fatal(err)
	}
	if VerifyAgentPlanProjection(nil, expected, binding) == nil || VerifyAgentPlanProjection(receipt, nil, binding) == nil {
		t.Fatal("absent proof or authority accepted")
	}
	cases := map[string]func(*agentv1.AgentPlanProjectionReceipt){
		"unrequested-planning-body": func(r *agentv1.AgentPlanProjectionReceipt) {
			r.Evidence.PlanningBody = proto.Clone(r.Evidence.StatusBody).(*agentv1.AgentArtifactReference)
		},
		"planning-flag": func(r *agentv1.AgentPlanProjectionReceipt) {
			r.Evidence.PlanningRequired = true
			r.Evidence.PlanningBody = proto.Clone(r.Evidence.StatusBody).(*agentv1.AgentArtifactReference)
		},
		"receipt-schema":  func(r *agentv1.AgentPlanProjectionReceipt) { r.SchemaVersion++ },
		"evidence-schema": func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.SchemaVersion++ },
		"source":          func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.SourceReceiptSha256 = strings.Repeat("d", 64) },
		"fence":           func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.SettlementFence.ExecutionEpoch++ },
		"counter":         func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.SettlementWriterToken++ },
		"plan":            func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.NormalizedPlan[0].Task = "other" },
		"nil-node":        func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.NormalizedPlan[0] = nil },
		"cursor":          func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.ExpectedIndex++ },
		"run":             func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.RunId = "other" },
		"status":          func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.NextStatus = "done" },
		"body":            func(r *agentv1.AgentPlanProjectionReceipt) { r.Evidence.PlanBody = nil },
		"empty-signature": func(r *agentv1.AgentPlanProjectionReceipt) { r.Signature = nil },
		"unknown": func(r *agentv1.AgentPlanProjectionReceipt) {
			r.Evidence.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
		},
	}
	for name, alter := range cases {
		t.Run(name, func(t *testing.T) {
			r := proto.Clone(receipt).(*agentv1.AgentPlanProjectionReceipt)
			alter(r)
			if name != "empty-signature" {
				sign(r)
			}
			if VerifyAgentPlanProjection(r, expected, binding) == nil {
				t.Fatal("rebound or noncanonical evidence accepted")
			}
		})
	}
	forged := proto.Clone(receipt).(*agentv1.AgentPlanProjectionReceipt)
	forged.Signature[0] ^= 1
	if VerifyAgentPlanProjection(forged, expected, binding) == nil {
		t.Fatal("forged signature accepted")
	}
	invalidKey := binding
	invalidKey.PublicKey = "invalid-key"
	forged = proto.Clone(receipt).(*agentv1.AgentPlanProjectionReceipt)
	forged.SignerPublicKey = invalidKey.PublicKey
	if VerifyAgentPlanProjection(forged, expected, invalidKey) == nil {
		t.Fatal("invalid public-key encoding accepted")
	}
}
