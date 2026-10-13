package store

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func synthesisProjectionSign(t *testing.T, receipt *agentv1.AgentSynthesisProjectionReceipt, private ed25519.PrivateKey) {
	t.Helper()
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(private, append([]byte(agentproof.SynthesisProjectionDomain), payload...))
}

func synthesisSettlementFixture(t *testing.T) (*Control, AgentSynthesisSettlementRequest, ed25519.PrivateKey) {
	t.Helper()
	control, complete, private := tasksCompletionSignedFixture(t, false)
	if _, err := control.CompleteAgentTasks(complete); err != nil {
		t.Fatal(err)
	}
	request := synthesisRequest(t, control)
	claim, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	lease := claim.Admission.Lease
	dispatch, err := control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{OperationID: "synthesis-dispatch", Actor: request.Actor,
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: request.RunID, NodeID: "synthesizer", Owner: lease.Owner, ClaimToken: lease.ClaimToken})
	if err != nil {
		t.Fatal(err)
	}
	intent := agentOutcomeIntent(dispatch.Intent)
	source := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentNodeOutcomeEvidence{SchemaVersion: 1, Fence: intent.Fence, OperationId: intent.OperationId,
			RunId: intent.RunId, NodeId: intent.NodeId, Phase: intent.Phase, Request: intent.Request, PlanDigest: intent.PlanDigest,
			MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch, WriterFencingToken: lease.WriterFencingToken,
			CreatedAt: time.Unix(intent.RecordedAt, 0).UTC().Format(time.RFC3339), ConfirmedAt: time.Unix(intent.RecordedAt+1, 0).UTC().Format(time.RFC3339),
			OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 52, PublicEvent: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 40},
			TaskOutput: &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 13, CompletionTokens: 21, LatencyMs: 120},
			FleetId:    control.fleetID, Environment: control.environment}}
	agentOutcomeSign(t, source, private)
	recorded, err := control.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{OperationID: "synthesis-source", Actor: request.Actor, ActionID: lease.ActionID,
		Epoch: lease.Epoch, RunID: request.RunID, NodeID: "synthesizer", Receipt: source})
	if err != nil {
		t.Fatal(err)
	}
	projection := &agentv1.AgentSynthesisProjectionReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentSynthesisProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: recorded.ReceiptSHA256,
			SettlementFence: &commonv1.ActionFence{ActionId: lease.ActionID, ExecutionEpoch: lease.Epoch}, SettlementWriterToken: control.token,
			RunId: request.RunID, MetadataEpoch: intent.MetadataEpoch, ExpectedIndex: intent.MetadataIndex, ContentBody: source.Evidence.PublicEvent,
			DoneBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 15}, FleetId: control.fleetID, Environment: control.environment}}
	synthesisProjectionSign(t, projection, private)
	return control, AgentSynthesisSettlementRequest{OperationID: "settle-synthesis", Actor: request.Actor, ActionID: lease.ActionID,
		Epoch: lease.Epoch, RunID: request.RunID, Owner: lease.Owner, ClaimToken: lease.ClaimToken, Projection: projection}, private
}

func synthesisSnapshot(t *testing.T, control *Control) string {
	t.Helper()
	snapshot, err := control.ExportSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	return snapshot.Digest
}

func TestSynthesisSettlementCommitsBothPublicEventsAndReplaysAfterReopen(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	before := countControlEvents(t, control)
	result, err := control.SettleAgentSynthesis(input)
	if err != nil || result.AlreadyApplied || result.Action.State != "SUCCEEDED" || result.Run.Status != "done" || result.Run.NextIndex != 8 ||
		result.Run.LastEvent.Type != "done" || result.Run.LastEvent.Body.SHA256 != input.Projection.Evidence.DoneBody.Sha256 || countControlEvents(t, control) != before+3 {
		t.Fatalf("final settlement was incomplete: %+v %v", result, err)
	}
	for _, node := range []string{"coder", "critic"} {
		if result.Run.Nodes()[node].State != "succeeded" {
			t.Fatal("final settlement lost task state")
		}
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 0 {
		t.Fatal("final settlement retained resources", err)
	}
	path, now, signer := control.path, control.now, control.agentOutcomeSignerKey
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "synthesis-reopened", Now: now, AuthorizeCutover: true,
		PromotionSignerPublicKey: control.promotionSignerKey, AgentOutcomeSignerPublicKey: signer, FleetID: control.fleetID, Environment: control.environment})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = reopened.Close() })
	replay, err := reopened.SettleAgentSynthesis(input)
	if err != nil || !replay.AlreadyApplied || replay.Run.Status != "done" || replay.Run.NextIndex != 8 || countControlEvents(t, reopened) != before+3 {
		t.Fatalf("reopened settlement replay duplicated or lost final events: %v", err)
	}
	_, _, receipt, found, err := reopened.AgentNodeOutcomeSnapshot(input.ActionID, "synthesizer")
	if err != nil || !found || receipt == nil {
		t.Fatalf("final proof unreadable after reopen: %v", err)
	}
}

func TestSynthesisSettlementRefusesReboundProofWithoutAnyWrite(t *testing.T) {
	cases := map[string]func(*AgentSynthesisSettlementRequest){
		"nil":      func(i *AgentSynthesisSettlementRequest) { i.Projection = nil },
		"action":   func(i *AgentSynthesisSettlementRequest) { i.ActionID = "" },
		"run":      func(i *AgentSynthesisSettlementRequest) { i.RunID = "" },
		"epoch":    func(i *AgentSynthesisSettlementRequest) { i.Epoch = 0 },
		"overflow": func(i *AgentSynthesisSettlementRequest) { i.Epoch = math.MaxUint64 },
		"owner":    func(i *AgentSynthesisSettlementRequest) { i.Owner = "other" },
		"token":    func(i *AgentSynthesisSettlementRequest) { i.ClaimToken = "other" },
		"unsigned": func(i *AgentSynthesisSettlementRequest) { i.Projection.Signature[0] ^= 1 },
		"source": func(i *AgentSynthesisSettlementRequest) {
			i.Projection.Evidence.SourceReceiptSha256 = strings.Repeat("d", 64)
		},
		"writer":   func(i *AgentSynthesisSettlementRequest) { i.Projection.Evidence.SettlementWriterToken++ },
		"fence":    func(i *AgentSynthesisSettlementRequest) { i.Projection.Evidence.SettlementFence.ExecutionEpoch++ },
		"cursor":   func(i *AgentSynthesisSettlementRequest) { i.Projection.Evidence.ExpectedIndex++ },
		"metadata": func(i *AgentSynthesisSettlementRequest) { i.Projection.Evidence.MetadataEpoch++ },
		"content": func(i *AgentSynthesisSettlementRequest) {
			i.Projection.Evidence.ContentBody = &agentv1.AgentArtifactReference{Sha256: strings.Repeat("d", 64), Length: 40}
		},
		"done":  func(i *AgentSynthesisSettlementRequest) { i.Projection.Evidence.DoneBody.Length = 0 },
		"realm": func(i *AgentSynthesisSettlementRequest) { i.Projection.Evidence.Environment = "other" },
		"unknown": func(i *AgentSynthesisSettlementRequest) {
			i.Projection.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
		},
	}
	for name, alter := range cases {
		t.Run(name, func(t *testing.T) {
			control, input, private := synthesisSettlementFixture(t)
			before := synthesisSnapshot(t, control)
			alter(&input)
			if input.Projection != nil && name != "unsigned" {
				synthesisProjectionSign(t, input.Projection, private)
			}
			if _, err := control.SettleAgentSynthesis(input); err == nil {
				t.Fatal("invalid final settlement succeeded")
			}
			if synthesisSnapshot(t, control) != before {
				t.Fatal("refused settlement changed durable control state")
			}
		})
	}
}

func TestSynthesisSettlementRollsBackFinalEventsAndLeaseChangesOnStorageFailure(t *testing.T) {
	for _, table := range []string{"control_events", "action_lease_events", "control_operator_mutations"} {
		t.Run(table, func(t *testing.T) {
			control, input, _ := synthesisSettlementFixture(t)
			before := synthesisSnapshot(t, control)
			if _, err := control.db.Exec("CREATE TEMP TRIGGER deny_synthesis_write BEFORE INSERT ON " + table + " BEGIN SELECT RAISE(ABORT,'injected synthesis failure'); END"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentSynthesis(input); err == nil || synthesisSnapshot(t, control) != before {
				t.Fatalf("failed transaction retained a partial final state: %v", err)
			}
			if _, err := control.db.Exec("DROP TRIGGER temp.deny_synthesis_write"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentSynthesis(input); err != nil {
				t.Fatalf("rollback prevented valid retry: %v", err)
			}
		})
	}
}

func TestSynthesisFinalMarkerCannotBeInstalledThroughGenericWriter(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	record, found, err := control.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(record.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(input.Projection)
	if err != nil {
		t.Fatal(err)
	}
	fields[agentSynthesisSettlementField], _ = json.Marshal(encoded)
	record.Payload, _ = json.Marshal(fields)
	record.Revision++
	record.State = "SUCCEEDED"
	before := synthesisSnapshot(t, control)
	if err := control.Put(record); err == nil || synthesisSnapshot(t, control) != before {
		t.Fatalf("generic writer installed final marker: %v", err)
	}
	// A changed replay request cannot turn an existing terminal action into a new effect.
	if _, err := control.SettleAgentSynthesis(input); err != nil {
		t.Fatal(err)
	}
	input.Projection.Signature = make([]byte, ed25519.SignatureSize)
	digest := sha256.Sum256(encoded)
	input.Projection.Evidence.SourceReceiptSha256 = hex.EncodeToString(digest[:])
	before = synthesisSnapshot(t, control)
	if _, err := control.SettleAgentSynthesis(input); err == nil || synthesisSnapshot(t, control) != before {
		t.Fatalf("changed terminal replay succeeded: %v", err)
	}
}

func TestSynthesisHistoricalFinalProjectionRejectsCoherentCheckpointTampering(t *testing.T) {
	for _, revision := range []int64{6, 7, 8} {
		for _, field := range []string{"missing", "status", "cursor", "body", "plan", "request", "timestamp"} {
			t.Run(strings.Join([]string{time.Unix(revision, 0).Format("05"), field}, "/"), func(t *testing.T) {
				control, input, _ := synthesisSettlementFixture(t)
				result, err := control.SettleAgentSynthesis(input)
				if err != nil {
					t.Fatal(err)
				}
				var change func(*Record)
				if field != "missing" {
					change = func(record *Record) {
						var run agent.RunMetadata
						if err := json.Unmarshal(record.Payload, &run); err != nil {
							t.Fatal(err)
						}
						switch field {
						case "status":
							run.Status = "failed"
						case "cursor":
							run.LastEvent.Index++
						case "body":
							run.LastEvent.Body.SHA256 = strings.Repeat("e", 64)
						case "plan":
							run.Plan[0].Task = "forged"
						case "request":
							run.Request.SHA256 = strings.Repeat("f", 64)
						case "timestamp":
							run.LastEvent.CreatedAt = "2000-01-01T00:00:00Z"
						}
						record.Payload, _ = json.Marshal(run)
					}
				}
				restore := taskBoundaryHistoryView(t, control, "agent_run", input.RunID, revision, change)
				tx, err := control.db.Begin()
				if err != nil {
					t.Fatal(err)
				}
				err = control.validateAgentSynthesisSettlementTx(tx, result.Action)
				_ = tx.Rollback()
				restore()
				if err == nil {
					t.Fatal("coherent altered final checkpoint authorized the historical projection")
				}
				tx, err = control.db.Begin()
				if err != nil {
					t.Fatal(err)
				}
				err = control.validateAgentSynthesisSettlementTx(tx, result.Action)
				_ = tx.Rollback()
				if err != nil {
					t.Fatalf("restoring original checkpoint did not restore proof: %v", err)
				}
			})
		}
	}
}
