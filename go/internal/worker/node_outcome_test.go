package worker

import (
	"context"
	"crypto/ed25519"
	"errors"
	"strings"
	"testing"
	"time"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

func signedNodeFixture(t *testing.T) (*agentv1.AgentNodeEffectMetadata, *agentv1.AgentNodeDispatchIntent, ControlSignerBinding, ed25519.PrivateKey) {
	t.Helper()
	_, binding, private := controlSigningFixture() // existing offline unit key only
	now := time.Now().UTC().Truncate(time.Second).Add(-10 * time.Second)
	intent := &agentv1.AgentNodeDispatchIntent{SchemaVersion: 1, OperationId: "agent-node-" + strings.Repeat("a", 64),
		Fence: &commonv1.ActionFence{ActionId: "agent-plan", ExecutionEpoch: 2}, RunId: "run", NodeId: "leader",
		Phase:      agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN,
		Request:    &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 30},
		PlanDigest: strings.Repeat("c", 64), MetadataIndex: 4, MetadataEpoch: 3, State: "INTENT_RECORDED", RecordedAt: now.Unix()}
	evidence := &agentv1.AgentNodeOutcomeEvidence{SchemaVersion: 1, Fence: proto.Clone(intent.Fence).(*commonv1.ActionFence),
		OperationId: intent.OperationId, RunId: intent.RunId, NodeId: intent.NodeId, Phase: intent.Phase,
		Request: proto.Clone(intent.Request).(*agentv1.AgentArtifactReference), PlanDigest: intent.PlanDigest,
		MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch, WriterFencingToken: 7,
		CreatedAt: now.Format("2006-01-02T15:04:05Z"), ConfirmedAt: now.Add(time.Second).Format("2006-01-02T15:04:05Z"),
		OutcomeSha256: strings.Repeat("d", 64), OutcomeLength: 100,
		PublicEvent:  &agentv1.AgentArtifactReference{Sha256: strings.Repeat("e", 64), Length: 50},
		ProviderPlan: []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "implement"}}, FleetId: binding.FleetID, Environment: binding.Environment}
	receipt := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, Evidence: evidence, SignerPublicKey: binding.PublicKey}
	resignNodeFixture(t, receipt, private)
	return &agentv1.AgentNodeEffectMetadata{OperationId: intent.OperationId, Fence: proto.Clone(intent.Fence).(*commonv1.ActionFence),
		RunId: intent.RunId, NodeId: intent.NodeId, State: "CONFIRMED", PublicEvent: proto.Clone(evidence.PublicEvent).(*agentv1.AgentArtifactReference),
		OutcomeSha256: evidence.OutcomeSha256, OutcomeLength: evidence.OutcomeLength, OutcomeReceipt: receipt}, intent, binding, private
}

func resignNodeFixture(t *testing.T, receipt *agentv1.AgentNodeOutcomeReceipt, private ed25519.PrivateKey) {
	t.Helper()
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(private, append([]byte(agentNodeOutcomeDomain), payload...))
}

func TestAgentNodeOutcomeHistoricalProofAndCopyIsolation(t *testing.T) {
	effect, intent, binding, private := signedNodeFixture(t)
	// A historical receipt survives lease expiry. It is not a live authorization.
	intent.RecordedAt -= 7200
	effect.OutcomeReceipt.Evidence.CreatedAt = time.Unix(intent.RecordedAt, 0).UTC().Format("2006-01-02T15:04:05Z")
	effect.OutcomeReceipt.Evidence.ConfirmedAt = time.Unix(intent.RecordedAt+1, 0).UTC().Format("2006-01-02T15:04:05Z")
	resignNodeFixture(t, effect.OutcomeReceipt, private)
	outcome, err := VerifyAgentNodeEffect(effect, intent, 7, binding)
	if err != nil || outcome.Evidence().RunId != intent.RunId {
		t.Fatalf("valid historical result rejected: %v", err)
	}
	outcome.Evidence().ProviderPlan[0].Task = "rebound"
	effect.OutcomeReceipt.Evidence.ProviderPlan[0].Task = "rebound"
	if outcome.Evidence().ProviderPlan[0].Task != "implement" {
		t.Fatal("caller changed verified evidence through an alias")
	}
	var missing *VerifiedAgentNodeOutcome
	if missing.Evidence() != nil || (&VerifiedAgentNodeOutcome{}).Evidence() != nil {
		t.Fatal("zero value fabricated verified evidence")
	}
}

func TestAgentNodeOutcomeRejectsSignedRebindingAndMalformedEvidence(t *testing.T) {
	unknown := []byte{0xa0, 0x06, 0x01}
	cases := map[string]func(*agentv1.AgentNodeOutcomeReceipt){
		"schema":          func(r *agentv1.AgentNodeOutcomeReceipt) { r.SchemaVersion++ },
		"evidence-schema": func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.SchemaVersion++ },
		"operation": func(r *agentv1.AgentNodeOutcomeReceipt) {
			r.Evidence.OperationId = "agent-node-" + strings.Repeat("f", 64)
		},
		"run":              func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.RunId = "other" },
		"node":             func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.NodeId = "coder" },
		"phase":            func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.Phase++ },
		"epoch":            func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.Fence.ExecutionEpoch++ },
		"action":           func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.Fence.ActionId = "other" },
		"request":          func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.Request.Length++ },
		"plan":             func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.PlanDigest = strings.Repeat("f", 64) },
		"index":            func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.MetadataIndex++ },
		"metadata-epoch":   func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.MetadataEpoch++ },
		"writer":           func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.WriterFencingToken++ },
		"fleet":            func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.FleetId = "other" },
		"environment":      func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.Environment = "other" },
		"length":           func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.OutcomeLength = 1024*1024 + 1 },
		"digest":           func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.OutcomeSha256 = strings.Repeat("D", 64) },
		"artifact":         func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.PublicEvent.Length = 128*1024*1024 + 1 },
		"missing-artifact": func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.PublicEvent = nil },
		"created":          func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.CreatedAt = "bad" },
		"fractional-time": func(r *agentv1.AgentNodeOutcomeReceipt) {
			r.Evidence.CreatedAt = strings.TrimSuffix(r.Evidence.CreatedAt, "Z") + ".1Z"
		},
		"old-created":  func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.CreatedAt = "2020-01-01T00:00:00Z" },
		"reverse-time": func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.ConfirmedAt = "2020-01-01T00:00:00Z" },
		"future-time": func(r *agentv1.AgentNodeOutcomeReceipt) {
			r.Evidence.ConfirmedAt = time.Now().UTC().Add(time.Hour).Format("2006-01-02T15:04:05Z")
		},
		"receipt-unknown":  func(r *agentv1.AgentNodeOutcomeReceipt) { r.ProtoReflect().SetUnknown(unknown) },
		"evidence-unknown": func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.ProtoReflect().SetUnknown(unknown) },
		"fence-unknown":    func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.Fence.ProtoReflect().SetUnknown(unknown) },
		"artifact-unknown": func(r *agentv1.AgentNodeOutcomeReceipt) { r.Evidence.PublicEvent.ProtoReflect().SetUnknown(unknown) },
		"plan-unknown": func(r *agentv1.AgentNodeOutcomeReceipt) {
			r.Evidence.ProviderPlan[0].ProtoReflect().SetUnknown(unknown)
		},
		"missing-node": func(r *agentv1.AgentNodeOutcomeReceipt) {
			r.Evidence.ProviderPlan = append(r.Evidence.ProviderPlan, nil)
		},
		"oversize": func(r *agentv1.AgentNodeOutcomeReceipt) {
			r.Evidence.ProviderPlan[0].Task = strings.Repeat("x", 1024*1024)
		},
	}
	for name, alter := range cases {
		t.Run(name, func(t *testing.T) {
			effect, intent, binding, private := signedNodeFixture(t)
			alter(effect.OutcomeReceipt)
			resignNodeFixture(t, effect.OutcomeReceipt, private)
			if _, err := VerifyAgentNodeOutcome(effect.OutcomeReceipt, intent, 7, binding); !errors.Is(err, ErrAgentNodeOutcomeInvalid) {
				t.Fatalf("accepted signed invalid evidence: %v", err)
			}
		})
	}
	for _, name := range []string{"signature", "key", "domain", "intent", "binding", "bad-key-encoding", "bad-key-length", "token", "nil"} {
		t.Run(name, func(t *testing.T) {
			effect, intent, binding, private := signedNodeFixture(t)
			token := int64(7)
			switch name {
			case "signature":
				effect.OutcomeReceipt.Signature[0] ^= 1
			case "key":
				effect.OutcomeReceipt.SignerPublicKey = "bad"
			case "domain":
				payload, _ := proto.MarshalOptions{Deterministic: true}.Marshal(effect.OutcomeReceipt.Evidence)
				effect.OutcomeReceipt.Signature = ed25519.Sign(private, payload)
			case "intent":
				intent.RecordedAt = 0
			case "binding":
				binding.PublicKey = "bad"
			case "bad-key-encoding":
				binding.PublicKey, effect.OutcomeReceipt.SignerPublicKey = "!", "!"
			case "bad-key-length":
				binding.PublicKey, effect.OutcomeReceipt.SignerPublicKey = "AA", "AA"
			case "token":
				token = 0
			case "nil":
				effect.OutcomeReceipt = nil
			}
			if _, err := VerifyAgentNodeOutcome(effect.OutcomeReceipt, intent, token, binding); !errors.Is(err, ErrAgentNodeOutcomeInvalid) {
				t.Fatalf("accepted invalid envelope: %v", err)
			}
		})
	}
}

func TestAgentNodeOutcomeRejectsReboundProjection(t *testing.T) {
	for _, name := range []string{"state", "reference", "hash", "length", "run", "node", "operation", "fence", "unknown"} {
		t.Run(name, func(t *testing.T) {
			effect, intent, binding, _ := signedNodeFixture(t)
			switch name {
			case "state":
				effect.State = "EFFECT_UNKNOWN"
			case "reference":
				effect.PublicEvent.Length++
			case "hash":
				effect.OutcomeSha256 = strings.Repeat("f", 64)
			case "length":
				effect.OutcomeLength++
			case "run":
				effect.RunId = "other"
			case "node":
				effect.NodeId = "other"
			case "operation":
				effect.OperationId = "other"
			case "fence":
				effect.Fence.ExecutionEpoch++
			case "unknown":
				effect.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
			}
			if _, err := VerifyAgentNodeEffect(effect, intent, 7, binding); !errors.Is(err, ErrAgentNodeOutcomeInvalid) {
				t.Fatalf("accepted rebound projection: %v", err)
			}
		})
	}
}

type fixtureNodeWorker struct {
	execute func(*agentv1.ExecuteAgentPlanRequest) (*agentv1.AgentNodeEffectMetadata, error)
	query   func(*agentv1.QueryAgentNodeEffectRequest) (*agentv1.QueryAgentNodeEffectResponse, error)
}

func (rpc fixtureNodeWorker) ExecutePlan(_ context.Context, input *agentv1.ExecuteAgentPlanRequest, _ ...grpc.CallOption) (*agentv1.AgentNodeEffectMetadata, error) {
	return rpc.execute(input)
}
func (rpc fixtureNodeWorker) QueryEffect(_ context.Context, input *agentv1.QueryAgentNodeEffectRequest, _ ...grpc.CallOption) (*agentv1.QueryAgentNodeEffectResponse, error) {
	return rpc.query(input)
}

func TestAgentNodeClientRequiresTLSAndPreservesUnknown(t *testing.T) {
	effect, intent, _, _ := signedNodeFixture(t)
	request := &agentv1.ExecuteAgentPlanRequest{SchemaVersion: 1, OperationId: "dispatch", Actor: "rust-agent-worker",
		Fence: intent.Fence, Claim: &agentv1.AgentRunExecutionClaim{Fence: intent.Fence, RunId: intent.RunId, Phase: intent.Phase}, SignedPhaseGrant: []byte("authority")}
	query := &agentv1.QueryAgentNodeEffectRequest{OperationId: intent.OperationId, Fence: intent.Fence, RunId: intent.RunId, NodeId: intent.NodeId}
	client := &Client{nodes: fixtureNodeWorker{execute: func(*agentv1.ExecuteAgentPlanRequest) (*agentv1.AgentNodeEffectMetadata, error) { return effect, nil },
		query: func(*agentv1.QueryAgentNodeEffectRequest) (*agentv1.QueryAgentNodeEffectResponse, error) {
			return &agentv1.QueryAgentNodeEffectResponse{Found: true, Effect: effect}, nil
		}}}
	if _, err := client.ExecuteAgentPlan(context.Background(), request); !errors.Is(err, ErrWorkerTLSRequired) {
		t.Fatal(err)
	}
	client.tlsSecured, client.bearerAttached = true, true
	if _, err := client.ExecuteAgentPlan(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if _, err := client.QueryAgentNodeEffect(context.Background(), query); err != nil {
		t.Fatal(err)
	}
	ctx := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer conflicting-offline-fixture")
	if _, err := client.ExecuteAgentPlan(ctx, request); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatal(err)
	}
	if _, err := client.ExecuteAgentPlan(nil, request); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal(err)
	}
	if _, err := client.ExecuteAgentPlan(context.Background(), nil); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal(err)
	}
	if _, err := client.QueryAgentNodeEffect(context.Background(), nil); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal(err)
	}
	effect.State, effect.PublicEvent, effect.OutcomeSha256, effect.OutcomeLength, effect.OutcomeReceipt = "EFFECT_UNKNOWN", nil, "", 0, nil
	if retained, err := client.QueryAgentNodeEffect(context.Background(), query); err != nil || retained.State != "EFFECT_UNKNOWN" {
		t.Fatal(err)
	}
	if _, err := client.ExecuteAgentPlan(context.Background(), request); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal(err)
	}
	effect.PublicEvent = &agentv1.AgentArtifactReference{Sha256: strings.Repeat("e", 64), Length: 1}
	if _, err := client.QueryAgentNodeEffect(context.Background(), query); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal(err)
	}
	client.nodes = fixtureNodeWorker{query: func(*agentv1.QueryAgentNodeEffectRequest) (*agentv1.QueryAgentNodeEffectResponse, error) {
		return &agentv1.QueryAgentNodeEffectResponse{}, nil
	}}
	if absent, err := client.QueryAgentNodeEffect(context.Background(), query); err != nil || absent != nil {
		t.Fatal(err)
	}
	client.nodes = fixtureNodeWorker{query: func(*agentv1.QueryAgentNodeEffectRequest) (*agentv1.QueryAgentNodeEffectResponse, error) {
		return nil, errors.New("transport")
	},
		execute: func(*agentv1.ExecuteAgentPlanRequest) (*agentv1.AgentNodeEffectMetadata, error) {
			return nil, errors.New("transport")
		}}
	if _, err := client.ExecuteAgentPlan(context.Background(), request); err == nil {
		t.Fatal("lost transport inferred success")
	}
	if _, err := client.QueryAgentNodeEffect(context.Background(), query); err == nil {
		t.Fatal("lost transport inferred absence")
	}
}
