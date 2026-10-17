package worker

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

type progressRPC struct {
	agentv1.AgentNodeWorkerClient
	result *agentv1.AgentRecoveryProgressReceipt
	err    error
	called int
}

func (rpc *progressRPC) PrepareRecoveryProgress(context.Context, *agentv1.PrepareAgentRecoveryProgressRequest, ...grpc.CallOption) (*agentv1.AgentRecoveryProgressReceipt, error) {
	rpc.called++
	return rpc.result, rpc.err
}

type legacyProgressRPC struct{ agentNodeClient }

func progressClientFixture(t *testing.T) (*Client, *progressRPC, *agentv1.PrepareAgentRecoveryProgressRequest) {
	t.Helper()
	reference := func(s string) *agentv1.AgentArtifactReference {
		return &agentv1.AgentArtifactReference{Sha256: strings.Repeat(s, 64), Length: 40}
	}
	scope := &agentv1.AgentRecoveryProgressContext{SchemaVersion: 1, RunId: "run", Generation: 1, MetadataEpoch: 3, ExpectedIndex: 12, RuntimeRevision: 2,
		Request: reference("a"), RecoveryContextSha256: strings.Repeat("b", 64), TaskActionId: "original-task", TaskActionRevision: 7, TaskMetadataIndex: 12,
		Role: "critic", Synthesize: true, Outputs: []*agentv1.AgentRunRecoveryEffectSource{{Dispatch: &agentv1.AgentNodeDispatchIntent{SchemaVersion: 1}}}}
	input := &agentv1.PrepareAgentRecoveryProgressRequest{SchemaVersion: 1, Context: scope, Fence: &commonv1.ActionFence{ActionId: scope.RunId, ExecutionEpoch: scope.MetadataEpoch}}
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{42}, ed25519.SeedSize)) // offline transport fixture only
	result := &agentv1.AgentRecoveryProgressReceipt{SchemaVersion: 1, SignerPublicKey: base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey)),
		Evidence: &agentv1.AgentRecoveryProgressEvidence{SchemaVersion: 1, Context: proto.Clone(scope).(*agentv1.AgentRecoveryProgressContext), NoteBody: reference("c"), NextBody: reference("d"), FleetId: "fleet", Environment: "test"}}
	wire, err := (proto.MarshalOptions{Deterministic: true}).Marshal(result.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	result.Signature = ed25519.Sign(private, append([]byte(agentproof.RecoveryProgressDomain), wire...))
	rpc := &progressRPC{result: result}
	return &Client{nodes: rpc, tlsSecured: true, bearerAttached: true}, rpc, input
}

func TestProgressClientAdmitsOnlyBoundMetadataOverItsSecuredChannel(t *testing.T) {
	client, rpc, input := progressClientFixture(t)
	if result, err := client.PrepareAgentRecoveryProgress(context.Background(), input); err != nil || !proto.Equal(result, rpc.result) {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*agentv1.PrepareAgentRecoveryProgressRequest){
		"schema":  func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.SchemaVersion = 0 },
		"context": func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context = nil },
		"fence":   func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Fence = nil },
		"unknown": func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.ProtoReflect().SetUnknown([]byte{0x80, 6, 1}) },
		"nested-unknown": func(r *agentv1.PrepareAgentRecoveryProgressRequest) {
			r.Context.Outputs[0].Dispatch.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
		},
		"context-schema": func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.SchemaVersion = 0 },
		"run":            func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.RunId = "" },
		"foreign-fence":  func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Fence.ActionId = "other" },
		"epoch":          func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Fence.ExecutionEpoch++ },
		"zero-epoch": func(r *agentv1.PrepareAgentRecoveryProgressRequest) {
			r.Context.MetadataEpoch = 0
			r.Fence.ExecutionEpoch = 0
		},
		"generation":  func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.Generation = 0 },
		"cursor":      func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.ExpectedIndex = 0 },
		"runtime":     func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.RuntimeRevision = 1 },
		"request":     func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.Request = nil },
		"digest":      func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.RecoveryContextSha256 = "bad" },
		"action":      func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.TaskActionId = "" },
		"revision":    func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.TaskActionRevision = 0 },
		"task-cursor": func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.TaskMetadataIndex++ },
		"role":        func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.Role = "leader" },
		"outputs":     func(r *agentv1.PrepareAgentRecoveryProgressRequest) { r.Context.Outputs = nil },
	} {
		t.Run(name, func(t *testing.T) {
			copy := proto.Clone(input).(*agentv1.PrepareAgentRecoveryProgressRequest)
			change(copy)
			before := rpc.called
			if _, err := client.PrepareAgentRecoveryProgress(context.Background(), copy); !errors.Is(err, ErrInvalidWorkerResponse) || rpc.called != before {
				t.Fatal("invalid metadata reached Rust RPC", err)
			}
		})
	}
	if _, err := client.PrepareAgentRecoveryProgress(context.Background(), nil); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal(err)
	}
	for _, secured := range []bool{true, false} {
		copy := *client
		copy.tlsSecured = secured
		copy.bearerAttached = !secured
		before := rpc.called
		if _, err := copy.PrepareAgentRecoveryProgress(context.Background(), input); err == nil || rpc.called != before {
			t.Fatal("unsecured progress transport admitted")
		}
	}
	legacy := *client
	legacy.nodes = legacyProgressRPC{}
	if _, err := legacy.PrepareAgentRecoveryProgress(context.Background(), input); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal("missing progress service accepted", err)
	}
}

func TestProgressClientVerifiesCanonicalReceiptIntegrityBeforeReturningItToThePinnedStore(t *testing.T) {
	client, rpc, input := progressClientFixture(t)
	valid := rpc.result
	for _, fault := range []string{"nil", "evidence", "signature", "binding", "context", "unknown", "body"} {
		rpc.result = proto.Clone(valid).(*agentv1.AgentRecoveryProgressReceipt)
		switch fault {
		case "nil":
			rpc.result = nil
		case "evidence":
			rpc.result.Evidence = nil
		case "signature":
			rpc.result.Signature[0] ^= 1
		case "binding":
			rpc.result.SignerPublicKey = "bad"
		case "context":
			rpc.result.Evidence.Context.ExpectedIndex++
		case "unknown":
			rpc.result.Evidence.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
		case "body":
			rpc.result.Evidence.NoteBody = nil
		}
		if _, err := client.PrepareAgentRecoveryProgress(context.Background(), input); !errors.Is(err, ErrInvalidWorkerResponse) {
			t.Fatal("invalid receipt integrity accepted", fault, err)
		}
	}
	rpc.result = valid
	rpc.err = context.DeadlineExceeded
	if _, err := client.PrepareAgentRecoveryProgress(context.Background(), input); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("transport failure was lost", err)
	}
}
