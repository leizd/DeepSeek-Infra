package agentproof

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"math"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func TestProgressUnknownFieldGuardRejectsPlainAndTypedNilMessages(t *testing.T) {
	var typedNil *agentv1.AgentRecoveryProgressReceipt
	if KnownRecoveryProgressMessage(nil) || KnownRecoveryProgressMessage(typedNil) {
		t.Fatal("a missing protobuf message was considered supported")
	}
}

func progressProofFixture(t *testing.T) (*agentv1.AgentRecoveryProgressReceipt, Binding, ed25519.PrivateKey) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	binding := Binding{PublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "fleet", Environment: "test"}
	reference := func(s string) *agentv1.AgentArtifactReference {
		return &agentv1.AgentArtifactReference{Sha256: strings.Repeat(s, 64), Length: 40}
	}
	context := &agentv1.AgentRecoveryProgressContext{SchemaVersion: 1, RunId: "run", Generation: 2, MetadataEpoch: 3,
		ExpectedIndex: 12, RuntimeRevision: 4, Request: reference("a"), RecoveryContextSha256: strings.Repeat("b", 64),
		TaskActionId: "original-task", TaskActionRevision: 7, TaskMetadataIndex: 12, Role: "critic", Synthesize: true,
		Outputs: []*agentv1.AgentRunRecoveryEffectSource{{Dispatch: &agentv1.AgentNodeDispatchIntent{SchemaVersion: 1}}}}
	return &agentv1.AgentRecoveryProgressReceipt{SchemaVersion: 1, SignerPublicKey: binding.PublicKey,
		Evidence: &agentv1.AgentRecoveryProgressEvidence{SchemaVersion: 1, Context: context, NoteBody: reference("c"), NextBody: reference("d"),
			FleetId: binding.FleetID, Environment: binding.Environment}}, binding, private
}

func progressProofSign(t *testing.T, receipt *agentv1.AgentRecoveryProgressReceipt, private ed25519.PrivateKey, domain string) {
	t.Helper()
	payload, err := (proto.MarshalOptions{Deterministic: true}).Marshal(receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(private, append([]byte(domain), payload...))
}

func TestRecoveryProgressProofRequiresFreshScopeSignerAndPurpose(t *testing.T) {
	receipt, binding, private := progressProofFixture(t)
	wanted := proto.Clone(receipt.Evidence.Context).(*agentv1.AgentRecoveryProgressContext)
	progressProofSign(t, receipt, private, RecoveryProgressDomain)
	if err := VerifyAgentRecoveryProgress(receipt, wanted, binding); err != nil {
		t.Fatal(err)
	}
	for _, domain := range []string{NodeOutcomeDomain, SynthesisProjectionDomain, ""} {
		progressProofSign(t, receipt, private, domain)
		if VerifyAgentRecoveryProgress(receipt, wanted, binding) == nil {
			t.Fatal("another signing purpose authorized metadata progress", domain)
		}
	}
	progressProofSign(t, receipt, private, RecoveryProgressDomain)
	if VerifyAgentRecoveryProgress(nil, wanted, binding) == nil || VerifyAgentRecoveryProgress(receipt, nil, binding) == nil {
		t.Fatal("missing proof or independent Go scope admitted")
	}
	for name, change := range map[string]func(*agentv1.AgentRecoveryProgressReceipt){
		"receipt-version": func(r *agentv1.AgentRecoveryProgressReceipt) { r.SchemaVersion = 2 },
		"no-evidence":     func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence = nil },
		"signature":       func(r *agentv1.AgentRecoveryProgressReceipt) { r.Signature[0] ^= 1 },
		"signer":          func(r *agentv1.AgentRecoveryProgressReceipt) { r.SignerPublicKey = "another-key" },
		"unknown-receipt": func(r *agentv1.AgentRecoveryProgressReceipt) { r.ProtoReflect().SetUnknown([]byte{0x80, 6, 1}) },
		"unknown-nested-source": func(r *agentv1.AgentRecoveryProgressReceipt) {
			r.Evidence.Context.Outputs[0].Dispatch.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
		},
		"unknown-list-source": func(r *agentv1.AgentRecoveryProgressReceipt) {
			r.Evidence.Context.Outputs[0].ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
		},
		"evidence-version": func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.SchemaVersion = 2 },
		"context-version":  func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.SchemaVersion = 2 },
		"generation":       func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.Generation = 0 },
		"epoch":            func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.MetadataEpoch = 0 },
		"cursor":           func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.ExpectedIndex = math.MaxInt64 },
		"runtime":          func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.RuntimeRevision = 1 },
		"request":          func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.Request.Length = 0 },
		"recovery-digest":  func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.RecoveryContextSha256 = "bad" },
		"action":           func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.TaskActionId = "" },
		"action-revision":  func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.TaskActionRevision = 0 },
		"task-cursor":      func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.TaskMetadataIndex++ },
		"role":             func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.Role = "" },
		"sources":          func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Context.Outputs = nil },
		"note":             func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.NoteBody = nil },
		"next":             func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.NextBody.Length = 0 },
		"fleet":            func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.FleetId = "other" },
		"environment":      func(r *agentv1.AgentRecoveryProgressReceipt) { r.Evidence.Environment = "other" },
		"stale-signed-scope": func(r *agentv1.AgentRecoveryProgressReceipt) {
			r.Evidence.Context.Role = "coder"
			progressProofSign(t, r, private, RecoveryProgressDomain)
		},
	} {
		t.Run(name, func(t *testing.T) {
			copy := proto.Clone(receipt).(*agentv1.AgentRecoveryProgressReceipt)
			change(copy)
			if VerifyAgentRecoveryProgress(copy, wanted, binding) == nil {
				t.Fatal("invalid or rebound metadata proof admitted")
			}
		})
	}
	for _, key := range []string{"", "a", binding.PublicKey + "=", base64.RawURLEncoding.EncodeToString([]byte("short"))} {
		copy := proto.Clone(receipt).(*agentv1.AgentRecoveryProgressReceipt)
		copy.SignerPublicKey = key
		bad := binding
		bad.PublicKey = key
		if VerifyAgentRecoveryProgress(copy, wanted, bad) == nil {
			t.Fatal("noncanonical configured key admitted")
		}
	}
}
