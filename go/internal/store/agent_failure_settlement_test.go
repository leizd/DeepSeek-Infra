package store

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func failureProjectionSign(t *testing.T, receipt *agentv1.AgentFailureProjectionReceipt, key ed25519.PrivateKey) {
	t.Helper()
	b, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(key, append([]byte(agentproof.FailureProjectionDomain), b...))
}

func failureSettlementFixture(t *testing.T, phase string) (*Control, AgentFailureSettlementRequest, ed25519.PrivateKey) {
	t.Helper()
	return failureSettlementOutcomeFixture(t, phase, "failure")
}

func failureSettlementOutcomeFixture(t *testing.T, phase, sourceKind string) (*Control, AgentFailureSettlementRequest, ed25519.PrivateKey) {
	t.Helper()
	var control *Control
	var request AgentRunExecutionRequest
	var private ed25519.PrivateKey
	if phase == "plan" {
		control = agentExecutionControl(t, true)
		for index, state := range []string{"created", "planning"} {
			m := eventMutation(t, control, int64(index), agent.RunEvent{Type: "run_status", Status: state})
			m.OperationID, m.RunID, m.Fence.ActionId = fmt.Sprintf("failure-event-%d", index), "run_failure", "run_failure"
			if index != 0 {
				m.Create = nil
			}
			if _, err := control.AppendAgentRunEvent(m); err != nil {
				t.Fatal(err)
			}
		}
		request = agentExecutionRequest(t, control)
		request.OperationID, request.RunID, request.Fence.ActionId = "failure-plan-claim", "run_failure", "run_failure"
		request.Phase, request.ExpectedIndex = "plan", 2
		public, key, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		private = key
		control.agentOutcomeSignerKey = base64.RawURLEncoding.EncodeToString(public)
	} else {
		var complete CompleteAgentTasksRequest
		control, complete, private = tasksCompletionSignedFixture(t, false)
		if _, err := control.CompleteAgentTasks(complete); err != nil {
			t.Fatal(err)
		}
		request = synthesisRequest(t, control)
	}
	claim, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	lease := claim.Admission.Lease
	node := "leader"
	if phase == "synthesis" {
		node = "synthesizer"
	}
	dispatch, err := control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{OperationID: "failure-dispatch", Actor: request.Actor,
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: request.RunID, NodeID: node, Owner: lease.Owner, ClaimToken: lease.ClaimToken})
	if err != nil {
		t.Fatal(err)
	}
	i := agentOutcomeIntent(dispatch.Intent)
	source := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentNodeOutcomeEvidence{SchemaVersion: 1, Fence: i.Fence, OperationId: i.OperationId, RunId: i.RunId,
			NodeId: i.NodeId, Phase: i.Phase, Request: i.Request, PlanDigest: i.PlanDigest, MetadataIndex: i.MetadataIndex,
			MetadataEpoch: i.MetadataEpoch, WriterFencingToken: lease.WriterFencingToken,
			CreatedAt: time.Unix(i.RecordedAt, 0).UTC().Format(time.RFC3339), ConfirmedAt: time.Unix(i.RecordedAt+1, 0).UTC().Format(time.RFC3339),
			OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 112, PublicEvent: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 92},
			Failure: &agentv1.AgentProviderFailureMetadata{SchemaVersion: 1, HttpStatus: 401, ResponseSha256: strings.Repeat("c", 64)},
			FleetId: control.fleetID, Environment: control.environment}}
	if sourceKind == "success" {
		source.Evidence.Failure = nil
		if phase == "synthesis" {
			source.Evidence.TaskOutput = &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 13, CompletionTokens: 21, LatencyMs: 120}
		}
	}
	agentOutcomeSign(t, source, private)
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(source)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(encoded)
	sourceDigest := hex.EncodeToString(digest[:])
	if sourceKind != "missing" {
		recorded, err := control.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{OperationID: "failure-source", Actor: request.Actor,
			ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: request.RunID, NodeID: node, Receipt: source})
		if err != nil {
			t.Fatal(err)
		}
		if recorded.ReceiptSHA256 != sourceDigest {
			t.Fatal("stored source identity changed")
		}
	}
	p := &agentv1.AgentFailureProjectionReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentFailureProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: sourceDigest,
			SettlementFence: &commonv1.ActionFence{ActionId: lease.ActionID, ExecutionEpoch: lease.Epoch}, SettlementWriterToken: control.token,
			RunId: request.RunID, Phase: i.Phase, MetadataEpoch: i.MetadataEpoch, ExpectedIndex: i.MetadataIndex,
			ErrorBody: source.Evidence.PublicEvent, FleetId: control.fleetID, Environment: control.environment}}
	failureProjectionSign(t, p, private)
	return control, AgentFailureSettlementRequest{OperationID: "settle-proved-failure", Actor: request.Actor, ActionID: lease.ActionID,
		Epoch: lease.Epoch, RunID: request.RunID, Owner: lease.Owner, ClaimToken: lease.ClaimToken, Projection: p}, private
}

func TestProvedFailureCommitsOneErrorAndReleasesOnlyItsPhaseThenReplays(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		t.Run(phase, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, phase)
			before := countControlEvents(t, control)
			result, err := control.SettleAgentFailure(input)
			if err != nil || result.AlreadyApplied || result.Action.State != "FAILED" || result.Run.Status != "failed" ||
				result.Run.NextIndex != input.Projection.Evidence.ExpectedIndex+1 || result.Run.LastEvent.Type != "error" ||
				result.Run.LastEvent.Body.SHA256 != input.Projection.Evidence.ErrorBody.Sha256 || countControlEvents(t, control) != before+2 {
				t.Fatalf("proved failure did not close atomically: %+v %v", result, err)
			}
			if phase == "synthesis" {
				for _, role := range []string{"coder", "critic"} {
					if result.Run.Nodes()[role].State != "succeeded" {
						t.Fatal("failure discarded completed tasks")
					}
				}
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 0 {
				t.Fatal("proved failure retained its resource", err)
			}
			stable := synthesisSnapshot(t, control)
			replay, err := control.SettleAgentFailure(input)
			if err != nil || !replay.AlreadyApplied || !reflect.DeepEqual(result.Run, replay.Run) || synthesisSnapshot(t, control) != stable {
				t.Fatalf("failure replay rewrote history or lost proof: %+v %v", replay, err)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if err := control.validateAgentRecoveryTerminalTx(tx, result.Action, phase); err != nil {
				t.Fatal("proved failure cannot enter explicit recovery", err)
			}
		})
	}
}

func TestProvedFailureRejectsReboundSignatureScopeWithoutPartialWrite(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		for _, change := range []string{"source", "body", "action", "epoch", "writer", "run", "phase", "cursor", "metadata", "fleet", "owner", "token", "signature"} {
			t.Run(phase+"/"+change, func(t *testing.T) {
				control, input, key := failureSettlementFixture(t, phase)
				before := synthesisSnapshot(t, control)
				e := input.Projection.Evidence
				switch change {
				case "source":
					e.SourceReceiptSha256 = strings.Repeat("d", 64)
				case "body":
					e.ErrorBody.Sha256 = strings.Repeat("d", 64)
				case "action":
					e.SettlementFence.ActionId = "other"
				case "epoch":
					e.SettlementFence.ExecutionEpoch++
				case "writer":
					e.SettlementWriterToken++
				case "run":
					e.RunId = "other"
				case "phase":
					e.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS
				case "cursor":
					e.ExpectedIndex++
				case "metadata":
					e.MetadataEpoch++
				case "fleet":
					e.FleetId = "other"
				case "owner":
					input.Owner = "other"
				case "token":
					input.ClaimToken = "other"
				}
				failureProjectionSign(t, input.Projection, key)
				if change == "signature" {
					input.Projection.Signature[0] ^= 1
				}
				if _, err := control.SettleAgentFailure(input); err == nil {
					t.Fatal("rebound failure admitted")
				}
				if synthesisSnapshot(t, control) != before {
					t.Fatal("refused failure changed durable state")
				}
				resources, err := control.GetResourceLeases(input.ActionID)
				if err != nil || len(resources) == 0 {
					t.Fatal("refused failure released resource", err)
				}
			})
		}
	}
}

func TestFailureSettlementRollsBackLateWritesAndKeepsOriginalReceiptForRetry(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		for _, fault := range []struct{ table, event string }{
			{"control_events", "INSERT"}, {"action_journal", "UPDATE"},
			{"action_leases", "UPDATE"}, {"action_resource_leases", "DELETE"},
			{"action_lease_events", "INSERT"}, {"control_operator_mutations", "INSERT"},
		} {
			t.Run(phase+"/"+fault.table, func(t *testing.T) {
				control, input, _ := failureSettlementFixture(t, phase)
				before := synthesisSnapshot(t, control)
				if _, err := control.db.Exec("CREATE TEMP TRIGGER deny_failure_write BEFORE " + fault.event + " ON " + fault.table + " BEGIN SELECT RAISE(ABORT,'injected failure settlement fault'); END"); err != nil {
					t.Fatal(err)
				}
				if _, err := control.SettleAgentFailure(input); err == nil {
					t.Fatal("injected write failure committed")
				}
				if synthesisSnapshot(t, control) != before {
					t.Fatal("late failure retained partial durable writes")
				}
				resources, err := control.GetResourceLeases(input.ActionID)
				if err != nil || len(resources) == 0 {
					t.Fatal("rollback lost its reservation", err)
				}
				if _, err := control.db.Exec("DROP TRIGGER temp.deny_failure_write"); err != nil {
					t.Fatal(err)
				}
				if _, err := control.SettleAgentFailure(input); err != nil {
					t.Fatal("retained source could not settle after rollback", err)
				}
			})
		}
	}
}

func TestFailureMarkerCannotEnterThroughGenericOrOtherBoundWriter(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		control, input, _ := failureSettlementFixture(t, phase)
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
		fields[agentFailureSettlementField], _ = json.Marshal(encoded)
		record.Payload, _ = json.Marshal(fields)
		record.State, record.Revision = "FAILED", record.Revision+1
		before := synthesisSnapshot(t, control)
		if err := control.Put(record); err == nil {
			t.Fatal("generic writer installed typed failure")
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		write, err := prepareControlRecordWrite(record, nil)
		if err != nil {
			_ = tx.Rollback()
			t.Fatal(err)
		}
		write.allowBoundLease, write.agentNodeDispatch = true, true
		err = control.putControlRecordTx(tx, write, control.now())
		_ = tx.Rollback()
		if err == nil || synthesisSnapshot(t, control) != before {
			t.Fatal("other bound writer installed typed failure", err)
		}
	}
}

func TestFailureReplaySurvivesWriterRestartAndRechecksRetainedProof(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		t.Run(phase, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, phase)
			result, err := control.SettleAgentFailure(input)
			if err != nil {
				t.Fatal(err)
			}
			path, now, signer := control.path, control.now, control.agentOutcomeSignerKey
			if err := control.Close(); err != nil {
				t.Fatal(err)
			}
			reopened, err := OpenControl(OpenOptions{Path: path, Owner: "failure-reopened", Now: now, AuthorizeCutover: true,
				PromotionSignerPublicKey: control.promotionSignerKey, AgentOutcomeSignerPublicKey: signer, FleetID: control.fleetID, Environment: control.environment})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = reopened.Close() })
			replay, err := reopened.SettleAgentFailure(input)
			if err != nil || !replay.AlreadyApplied || !reflect.DeepEqual(result.Run, replay.Run) {
				t.Fatalf("reopened failure replay changed its result: %v", err)
			}
			_, _, receipt, found, err := reopened.AgentNodeOutcomeSnapshot(input.ActionID, agentFailureNode(phase))
			if err != nil || !found || receipt == nil || !agentproof.ValidTerminalFailure(receipt.Evidence) {
				t.Fatal("reopened failure lost original receipt", err)
			}
		})
	}
}

func TestFailureSettlementLosingLeaseBeforeCommitRollsBackAndKeepsOriginalSource(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		for _, expired := range []string{"action", "writer"} {
			t.Run(phase+"/"+expired, func(t *testing.T) {
				control, input, _ := failureSettlementFixture(t, phase)
				before := synthesisSnapshot(t, control)
				originalClock := control.now
				initial := originalClock()
				deadline := control.leaseUntil
				want := ErrWriterFenceHeld
				if expired == "action" {
					if err := control.db.QueryRow("SELECT lease_until FROM action_leases WHERE action_id=?", input.ActionID).Scan(&deadline); err != nil {
						t.Fatal(err)
					}
					if deadline < control.leaseUntil {
						want = ErrActionLeaseExpired
					}
				}
				reads := 0
				control.now = func() int64 {
					reads++
					if reads == 1 {
						return initial
					}
					return deadline
				}
				_, err := control.SettleAgentFailure(input)
				control.now = originalClock
				if !errors.Is(err, want) || synthesisSnapshot(t, control) != before {
					t.Fatalf("late %s loss left a partial failure settlement: %v", expired, err)
				}
				if _, err := control.SettleAgentFailure(input); err != nil {
					t.Fatal("rollback lost the original signed source", err)
				}
			})
		}
	}
}

func TestFailureHistoricalProjectionRejectsCoherentOriginalOrErrorCheckpointTampering(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		for _, offset := range []int64{0, 1} {
			for _, field := range []string{"missing", "status", "cursor", "body", "plan", "request", "timestamp", "nodes"} {
				t.Run(fmt.Sprintf("%s/%d/%s", phase, offset, field), func(t *testing.T) {
					control, input, _ := failureSettlementFixture(t, phase)
					result, err := control.SettleAgentFailure(input)
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
								run.Status = "done"
							case "cursor":
								run.LastEvent.Index++
							case "body":
								run.LastEvent.Body.SHA256 = strings.Repeat("e", 64)
							case "plan":
								run.Plan = []agent.RunPlanNode{{ID: "coder", Task: "forged failure plan"}}
							case "request":
								run.Request.SHA256 = strings.Repeat("f", 64)
							case "timestamp":
								run.LastEvent.CreatedAt = "2000-01-01T00:00:00Z"
							case "nodes":
								run.BaseNodes = map[string]agent.RunNode{"coder": {ID: "coder", State: "succeeded"}}
							}
							record.Payload, _ = json.Marshal(run)
						}
					}
					restore := taskBoundaryHistoryView(t, control, "agent_run", input.RunID, input.Projection.Evidence.ExpectedIndex+offset, change)
					tx, err := control.db.Begin()
					if err != nil {
						t.Fatal(err)
					}
					err = control.validateAgentFailureSettlementTx(tx, result.Action)
					_ = tx.Rollback()
					restore()
					if err == nil {
						t.Fatal("coherent altered checkpoint authorized a historical failure")
					}
					tx, err = control.db.Begin()
					if err != nil {
						t.Fatal(err)
					}
					err = control.validateAgentFailureSettlementTx(tx, result.Action)
					_ = tx.Rollback()
					if err != nil {
						t.Fatal("restoring the original checkpoint did not restore proof", err)
					}
				})
			}
		}
	}
}

func TestConfirmedPlannerFailureResumeKeepsLegacyDefaultDAGAndOriginalPublicPlan(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	failed, err := control.SettleAgentFailure(input)
	if err != nil {
		t.Fatal(err)
	}
	cutover, err := control.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	preview, err := control.PreviewAgentRunRecovery(AgentRunRecoveryExpected{RunID: failed.Run.RunID,
		Fence:         &internalprotocol.ActionFence{ActionId: failed.Run.RunID, ExecutionEpoch: uint64(cutover.Epoch)},
		ExpectedIndex: failed.Run.NextIndex, ExpectedRuntimeRevision: failed.Run.RuntimeVersion(), ExpectedRequest: failed.Run.Request,
		Selection: &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME}})
	if err != nil {
		t.Fatal("proved planner failure could not prepare explicit recovery", err)
	}
	context := preview.Context
	if context == nil || len(context.Plan) != 4 || len(context.Execute) != 4 || !context.Synthesize || !context.ResetFinal || context.CompleteImmediately {
		t.Fatal("planner failure lost the legacy default task DAG", context)
	}
	ids := []string{}
	for _, node := range context.Execute {
		ids = append(ids, node.Id)
	}
	if strings.Join(ids, ",") != "researcher,coder,reasoner,critic" {
		t.Fatal("planner failure changed default role order", ids)
	}
	if len(context.PriorEffects) != 1 || context.PriorEffects[0].Outcome == nil ||
		!agentproof.ValidTerminalFailure(context.PriorEffects[0].Outcome.Evidence) {
		t.Fatal("default task recovery omitted the original terminal failure proof")
	}
	after, found, err := control.AgentRunMetadata(failed.Run.RunID)
	if err != nil || !found || len(after.Plan) != 0 || after.NextIndex != failed.Run.NextIndex || after.Request != failed.Run.Request {
		t.Fatal("read-only preview replaced the original public plan snapshot", err)
	}
}
