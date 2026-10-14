package agentproof

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"math"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func TestSynthesisProjectionRequiresOwnSignatureAndOriginalContentScope(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	binding := Binding{PublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "fleet", Environment: "test"}
	wanted := &agentv1.AgentSynthesisProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: strings.Repeat("a", 64),
		SettlementFence: &commonv1.ActionFence{ActionId: "synthesis-action", ExecutionEpoch: 2}, SettlementWriterToken: 3,
		RunId: "run", MetadataEpoch: 1, ExpectedIndex: 6, ContentBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 40},
		FleetId: binding.FleetID, Environment: binding.Environment}
	receipt := &agentv1.AgentSynthesisProjectionReceipt{SchemaVersion: 1, SignerPublicKey: binding.PublicKey, Evidence: proto.Clone(wanted).(*agentv1.AgentSynthesisProjectionEvidence)}
	receipt.Evidence.DoneBody = &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 15}
	sign := func(r *agentv1.AgentSynthesisProjectionReceipt, domain string) {
		raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(r.Evidence)
		if err != nil {
			t.Fatal(err)
		}
		r.Signature = ed25519.Sign(private, append([]byte(domain), raw...))
	}
	sign(receipt, SynthesisProjectionDomain)
	if err := VerifyAgentSynthesisProjection(receipt, wanted, binding); err != nil {
		t.Fatal(err)
	}
	if VerifyAgentSynthesisProjection(nil, wanted, binding) == nil || VerifyAgentSynthesisProjection(receipt, nil, binding) == nil {
		t.Fatal("missing independent scope or proof admitted")
	}
	for name, change := range map[string]func(*agentv1.AgentSynthesisProjectionReceipt){
		"receipt-schema":  func(r *agentv1.AgentSynthesisProjectionReceipt) { r.SchemaVersion = 0 },
		"no-evidence":     func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence = nil },
		"receipt-unknown": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"evidence-unknown": func(r *agentv1.AgentSynthesisProjectionReceipt) {
			r.Evidence.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"evidence-schema": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SchemaVersion = 0 },
		"source": func(r *agentv1.AgentSynthesisProjectionReceipt) {
			r.Evidence.SourceReceiptSha256 = strings.Repeat("d", 64)
		},
		"source-invalid": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SourceReceiptSha256 = "bad" },
		"fence":          func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SettlementFence = nil },
		"fence-unknown": func(r *agentv1.AgentSynthesisProjectionReceipt) {
			r.Evidence.SettlementFence.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"action":          func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SettlementFence.ActionId = "other" },
		"epoch":           func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SettlementFence.ExecutionEpoch++ },
		"zero-epoch":      func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SettlementFence.ExecutionEpoch = 0 },
		"writer":          func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SettlementWriterToken++ },
		"zero-writer":     func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SettlementWriterToken = 0 },
		"run":             func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.RunId = "other" },
		"metadata-epoch":  func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.MetadataEpoch++ },
		"cursor":          func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.ExpectedIndex++ },
		"negative-cursor": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.ExpectedIndex = -1 },
		"overflow-cursor": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.ExpectedIndex = math.MaxInt64 },
		"content": func(r *agentv1.AgentSynthesisProjectionReceipt) {
			r.Evidence.ContentBody.Sha256 = strings.Repeat("d", 64)
		},
		"missing-content": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.ContentBody = nil },
		"content-unknown": func(r *agentv1.AgentSynthesisProjectionReceipt) {
			r.Evidence.ContentBody.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"missing-done": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.DoneBody = nil },
		"done-length":  func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.DoneBody.Length = 0 },
		"done-unknown": func(r *agentv1.AgentSynthesisProjectionReceipt) {
			r.Evidence.DoneBody.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
		"fleet":       func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.FleetId = "foreign" },
		"environment": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.Environment = "foreign" },
	} {
		t.Run(name, func(t *testing.T) {
			bad := proto.Clone(receipt).(*agentv1.AgentSynthesisProjectionReceipt)
			change(bad)
			if bad.Evidence != nil {
				sign(bad, SynthesisProjectionDomain)
			}
			if VerifyAgentSynthesisProjection(bad, wanted, binding) == nil {
				t.Fatal("correctly signed but rebound/invalid synthesis projection admitted")
			}
		})
	}
	wrong := proto.Clone(receipt).(*agentv1.AgentSynthesisProjectionReceipt)
	sign(wrong, PlanProjectionDomain)
	if VerifyAgentSynthesisProjection(wrong, wanted, binding) == nil {
		t.Fatal("planner signature domain authorized final synthesis")
	}
	for _, key := range []string{"malformed", base64.RawURLEncoding.EncodeToString([]byte{1})} {
		bad := proto.Clone(receipt).(*agentv1.AgentSynthesisProjectionReceipt)
		bad.SignerPublicKey = key
		other := binding
		other.PublicKey = key
		if VerifyAgentSynthesisProjection(bad, wanted, other) == nil {
			t.Fatal("invalid signing key admitted")
		}
	}
	big := proto.Clone(receipt).(*agentv1.AgentSynthesisProjectionReceipt)
	expected := proto.Clone(wanted).(*agentv1.AgentSynthesisProjectionEvidence)
	big.Evidence.RunId = strings.Repeat("x", 1024*1024)
	expected.RunId = big.Evidence.RunId
	sign(big, SynthesisProjectionDomain)
	if VerifyAgentSynthesisProjection(big, expected, binding) == nil {
		t.Fatal("oversized signed projection admitted")
	}
}
