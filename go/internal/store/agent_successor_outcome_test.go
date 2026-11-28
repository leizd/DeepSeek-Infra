package store

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
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

func bindAndTakeOverSuccessor(
	t *testing.T,
	control *Control,
	input AgentNodeDispatchRequest,
	begun AgentNodeDispatchResult,
	phase string,
	expectedIndex int64,
	dispatchSuccessor bool,
) (*Control, ActionLease, AgentNodeSuccessorBindResult, AgentNodeDispatchResult, ed25519.PrivateKey) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signerKey := base64.RawURLEncoding.EncodeToString(public)
	control.agentOutcomeSignerKey = signerKey

	bindReq := successorBindRequest(input)
	bindReq.OperationID = fmt.Sprintf("bind-%s-%s", phase, input.NodeID)
	bound, err := control.BindAgentNodeSuccessor(bindReq)
	if err != nil {
		t.Fatalf("bind successor: %v", err)
	}

	now := begun.Admission.Lease.LeaseUntil + 1
	options := OpenOptions{
		Path:                     control.path,
		Owner:                    "successor-control",
		Now:                      func() int64 { return now },
		AuthorizeCutover:         true,
		PromotionSignerPublicKey: control.promotionSignerKey,
		FleetID:                  control.fleetID,
		Environment:              control.environment,
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = successor.Close() })
	successor.agentOutcomeSignerKey = signerKey

	claimReq := agentExecutionRequest(t, successor)
	claimReq.OperationID = "successor-takeover-" + phase
	claimReq.Owner = "successor-worker"
	claimReq.RunID = input.RunID
	claimReq.Fence.ActionId = input.RunID
	claimReq.Phase = phase
	claimReq.ExpectedIndex = expectedIndex
	taken, err := successor.ClaimAgentRunExecution(claimReq)
	if err != nil {
		t.Fatalf("takeover claim: %v", err)
	}
	lease := taken.Admission.Lease
	if _, err := successor.AdmitSuccessorDispatch(AgentSuccessorEpochRequest{
		OperationID:       "admit-successor-" + phase,
		Actor:             input.Actor,
		ActionID:          input.ActionID,
		Epoch:             lease.Epoch,
		RunID:             input.RunID,
		NodeID:            input.NodeID,
		Owner:             lease.Owner,
		ClaimToken:        lease.ClaimToken,
		Effect:            "CONFIRMED",
		ResetAdmitted:     true,
		ResetFenceMatches: true,
		ResetIndex:        bound.ResetIndex,
	}); err != nil {
		t.Fatalf("admit successor: %v", err)
	}

	var dispatched AgentNodeDispatchResult
	if dispatchSuccessor {
		dispatched, err = successor.BeginAgentNodeDispatch(AgentNodeDispatchRequest{
			OperationID: bound.SuccessorOperationID,
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			Epoch:       lease.Epoch,
			RunID:       input.RunID,
			NodeID:      input.NodeID,
			Owner:       lease.Owner,
			ClaimToken:  lease.ClaimToken,
		})
		if err != nil {
			t.Fatalf("dispatch successor: %v", err)
		}
		if !dispatched.Fresh || dispatched.Intent.OperationID != bound.SuccessorOperationID ||
			dispatched.Intent.ExecutionEpoch != lease.Epoch || dispatched.Intent.RecordedAt != begun.Intent.RecordedAt {
			t.Fatalf("unexpected successor dispatch result: %+v", dispatched)
		}
	}
	return successor, lease, bound, dispatched, private
}

func buildSignedTaskSuccessorReceipt(
	t *testing.T,
	control *Control,
	intent AgentNodeDispatch,
	writerToken int64,
	private ed25519.PrivateKey,
) *agentv1.AgentNodeOutcomeReceipt {
	t.Helper()
	protoIntent := agentOutcomeIntent(intent)
	receipt := &agentv1.AgentNodeOutcomeReceipt{
		SchemaVersion:   1,
		SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentNodeOutcomeEvidence{
			SchemaVersion:      1,
			Fence:              protoIntent.Fence,
			OperationId:        protoIntent.OperationId,
			RunId:              protoIntent.RunId,
			NodeId:             protoIntent.NodeId,
			Phase:              protoIntent.Phase,
			Request:            protoIntent.Request,
			PlanDigest:         protoIntent.PlanDigest,
			MetadataIndex:      protoIntent.MetadataIndex,
			MetadataEpoch:      protoIntent.MetadataEpoch,
			WriterFencingToken: writerToken,
			CreatedAt:          time.Unix(protoIntent.RecordedAt, 0).UTC().Format(time.RFC3339),
			ConfirmedAt:        time.Unix(protoIntent.RecordedAt+1, 0).UTC().Format(time.RFC3339),
			OutcomeSha256:      strings.Repeat("a", 64),
			OutcomeLength:      30,
			PublicEvent:        &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 20},
			TaskOutput:         &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 13, CompletionTokens: 21, LatencyMs: 120},
			FleetId:            control.fleetID,
			Environment:        control.environment,
		},
	}
	agentOutcomeSign(t, receipt, private)
	return receipt
}

func TestSuccessorOutcomeRecordsReplaysSettlesTaskAndSurvivesRestart(t *testing.T) {
	control, input, begun := successorReadyFixture(t)
	successor, lease, bound, dispatched, private := bindAndTakeOverSuccessor(t, control, input, begun, "tasks", 4, true)
	receipt := buildSignedTaskSuccessorReceipt(t, successor, dispatched.Intent, lease.WriterFencingToken, private)

	outcomeReq := AgentNodeOutcomeRequest{
		OperationID: "record-successor-coder",
		Actor:       input.Actor,
		ActionID:    input.ActionID,
		Epoch:       lease.Epoch,
		RunID:       input.RunID,
		NodeID:      input.NodeID,
		Receipt:     receipt,
	}
	staleReq := outcomeReq
	staleReq.Epoch = input.Epoch
	if _, err := successor.RecordAgentNodeOutcome(staleReq); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("stale epoch recorded outcome: %v", err)
	}

	recorded, err := successor.RecordAgentNodeOutcome(outcomeReq)
	if err != nil || recorded.AlreadyRecorded || recorded.ReceiptSHA256 == "" {
		t.Fatalf("record successor outcome failed: %+v err=%v", recorded, err)
	}
	replay, err := successor.RecordAgentNodeOutcome(outcomeReq)
	if err != nil || !replay.AlreadyRecorded || replay.ReceiptSHA256 != recorded.ReceiptSHA256 {
		t.Fatalf("replay successor outcome drifted: %+v err=%v", replay, err)
	}

	// Submitting the original epoch-1 receipt after the successor settled must fail closed.
	origIntent := begun.Intent
	origIntent.OperationID = begun.Intent.OperationID
	origIntent.ExecutionEpoch = begun.Intent.ExecutionEpoch
	origReceipt := buildSignedTaskSuccessorReceipt(t, successor, origIntent, begun.Admission.Lease.WriterFencingToken, private)
	if _, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
		OperationID: "conflicting-original-coder",
		Actor:       input.Actor,
		ActionID:    input.ActionID,
		Epoch:       lease.Epoch,
		RunID:       input.RunID,
		NodeID:      input.NodeID,
		Receipt:     origReceipt,
	}); !errors.Is(err, ErrAgentNodeOutcomeConflict) {
		t.Fatalf("conflicting original receipt was not rejected: %v", err)
	}

	_, snapIntent, snapReceipt, found, err := successor.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || snapIntent.OperationID != bound.SuccessorOperationID ||
		snapIntent.ExecutionEpoch != lease.Epoch || !proto.Equal(snapReceipt, receipt) {
		t.Fatalf("unexpected outcome snapshot: intent=%+v found=%v err=%v", snapIntent, found, err)
	}
	_, origDispatch, found, err := successor.AgentNodeDispatchSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || origDispatch.OperationID != begun.Intent.OperationID ||
		origDispatch.ExecutionEpoch != begun.Intent.ExecutionEpoch || origDispatch.SuccessorOperationID != bound.SuccessorOperationID {
		t.Fatalf("original dispatch custody drifted: %+v found=%v err=%v", origDispatch, found, err)
	}

	settled, err := successor.SettleAgentTask(AgentTaskSettlementRequest{
		OperationID:         "settle-successor-coder",
		Actor:               input.Actor,
		ActionID:            input.ActionID,
		Epoch:               lease.Epoch,
		RunID:               input.RunID,
		NodeID:              input.NodeID,
		Owner:               lease.Owner,
		ClaimToken:          lease.ClaimToken,
		ExpectedIndex:       4,
		SourceReceiptSHA256: recorded.ReceiptSHA256,
	})
	if err != nil || settled.AlreadyApplied || !settled.Run.Nodes()[input.NodeID].TaskOutputSettled() ||
		settled.Run.Nodes()[input.NodeID].State != "succeeded" {
		t.Fatalf("task settlement on successor outcome failed: %+v err=%v", settled, err)
	}

	restartedOpts := OpenOptions{
		Path:                     successor.path,
		Owner:                    "restarted-control",
		Now:                      func() int64 { return lease.AcquiredAt + 2 },
		AuthorizeCutover:         true,
		PromotionSignerPublicKey: successor.promotionSignerKey,
		FleetID:                  successor.fleetID,
		Environment:              successor.environment,
	}
	signerKey := successor.agentOutcomeSignerKey
	if err := successor.Close(); err != nil {
		t.Fatal(err)
	}
	restarted, err := OpenControl(restartedOpts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = restarted.Close() })
	restarted.agentOutcomeSignerKey = signerKey

	_, restartedIntent, restartedReceipt, found, err := restarted.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found || restartedIntent.OperationID != bound.SuccessorOperationID ||
		restartedIntent.ExecutionEpoch != lease.Epoch || !proto.Equal(restartedReceipt, receipt) {
		t.Fatalf("restarted snapshot lost successor proof: intent=%+v found=%v err=%v", restartedIntent, found, err)
	}
	actionRec, found, err := restarted.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatalf("reloaded action missing: found=%v err=%v", found, err)
	}
	nodes, err := readAgentNodeDispatches(actionRec)
	if err != nil {
		t.Fatal(err)
	}
	if outs, err := restarted.agentNodeOutcomes(actionRec, nodes); err != nil || len(outs) != 1 {
		t.Fatalf("standalone reader rejected successor proof: len=%d err=%v", len(outs), err)
	}
}

func TestSuccessorOutcomeRefusalsBeforeDispatchOnBadFencesAndRetainsOriginalProof(t *testing.T) {
	t.Run("before_successor_dispatch", func(t *testing.T) {
		control, input, begun := successorReadyFixture(t)
		successor, lease, bound, _, private := bindAndTakeOverSuccessor(t, control, input, begun, "tasks", 4, false)
		unDispatchedIntent := begun.Intent
		unDispatchedIntent.OperationID = bound.SuccessorOperationID
		unDispatchedIntent.ExecutionEpoch = lease.Epoch
		receipt := buildSignedTaskSuccessorReceipt(t, successor, unDispatchedIntent, lease.WriterFencingToken, private)

		if _, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
			OperationID: "undispatched-successor",
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			Epoch:       lease.Epoch,
			RunID:       input.RunID,
			NodeID:      input.NodeID,
			Receipt:     receipt,
		}); !errors.Is(err, ErrAgentNodeDispatchRequired) {
			t.Fatalf("un-dispatched successor receipt returned %v, want %v", err, ErrAgentNodeDispatchRequired)
		}

		// Even if an operator reuses SuccessorOperationID for a non-dispatch mutation
		// (e.g., an agent_run event), RecordAgentNodeOutcome still refuses without
		// BeginAgentNodeDispatch provenance.
		run, found, err := successor.AgentRunMetadata(input.RunID)
		if err != nil || !found {
			t.Fatal(err)
		}
		cutover, err := successor.GetCutover("agent_run")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := successor.AppendAgentRunEvent(AgentRunEventMutation{
			OperationID:   bound.SuccessorOperationID,
			Actor:         input.Actor,
			RunID:         input.RunID,
			ExpectedIndex: run.NextIndex,
			Fence:         &internalprotocol.ActionFence{ActionId: input.RunID, ExecutionEpoch: uint64(cutover.Epoch)},
			Event:         agent.RunEvent{Type: "run_status", Status: "cancelled", Body: agent.ArtifactRef{SHA256: strings.Repeat("d", 64), Length: 20}},
		}); err != nil {
			t.Fatalf("append run event with colliding op id: %v", err)
		}
		if _, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
			OperationID: "undispatched-after-run-event",
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			Epoch:       lease.Epoch,
			RunID:       input.RunID,
			NodeID:      input.NodeID,
			Receipt:     receipt,
		}); !errors.Is(err, ErrAgentNodeDispatchRequired) {
			t.Fatalf("non-dispatch mutation fooled outcome provenance: %v", err)
		}
	})

	t.Run("fences_tokens_and_original_retake", func(t *testing.T) {
		control, input, begun := successorReadyFixture(t)
		successor, lease, _, dispatched, private := bindAndTakeOverSuccessor(t, control, input, begun, "tasks", 4, true)

		cases := []struct {
			name   string
			mutate func(AgentNodeDispatch, int64) (AgentNodeDispatch, int64)
		}{
			{
				name: "successor_id_on_original_epoch",
				mutate: func(intent AgentNodeDispatch, token int64) (AgentNodeDispatch, int64) {
					intent.ExecutionEpoch = begun.Intent.ExecutionEpoch
					return intent, begun.Admission.Lease.WriterFencingToken
				},
			},
			{
				name: "successor_id_on_unissued_future_epoch",
				mutate: func(intent AgentNodeDispatch, token int64) (AgentNodeDispatch, int64) {
					intent.ExecutionEpoch = lease.Epoch + 1
					return intent, token
				},
			},
			{
				name: "successor_id_with_wrong_writer_token",
				mutate: func(intent AgentNodeDispatch, token int64) (AgentNodeDispatch, int64) {
					return intent, token + 1
				},
			},
			{
				name: "original_id_on_takeover_epoch",
				mutate: func(intent AgentNodeDispatch, token int64) (AgentNodeDispatch, int64) {
					intent.OperationID = begun.Intent.OperationID
					return intent, token
				},
			},
		}
		for _, tc := range cases {
			t.Run(tc.name, func(t *testing.T) {
				badIntent, badToken := tc.mutate(dispatched.Intent, lease.WriterFencingToken)
				badReceipt := buildSignedTaskSuccessorReceipt(t, successor, badIntent, badToken, private)
				if _, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
					OperationID: "bad-" + tc.name,
					Actor:       input.Actor,
					ActionID:    input.ActionID,
					Epoch:       lease.Epoch,
					RunID:       input.RunID,
					NodeID:      input.NodeID,
					Receipt:     badReceipt,
				}); !errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) {
					t.Fatalf("%s returned %v, want %v", tc.name, err, agentproof.ErrAgentNodeOutcomeInvalid)
				}
			})
		}

		// Even after takeover and successor dispatch, an unrecorded original epoch-1
		// receipt still verifies and records under the current lease.
		origReceipt := buildSignedTaskSuccessorReceipt(t, successor, begun.Intent, begun.Admission.Lease.WriterFencingToken, private)
		res, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
			OperationID: "late-original-after-successor-dispatch",
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			Epoch:       lease.Epoch,
			RunID:       input.RunID,
			NodeID:      input.NodeID,
			Receipt:     origReceipt,
		})
		if err != nil || res.AlreadyRecorded {
			t.Fatalf("original proof rejected after successor dispatch: %+v err=%v", res, err)
		}
	})
}

func planReadyFixture(t *testing.T, runID string) (*Control, AgentNodeDispatchRequest, AgentNodeDispatchResult) {
	t.Helper()
	control := agentExecutionControl(t, true)
	for index, state := range []string{"created", "planning"} {
		m := eventMutation(t, control, int64(index), agent.RunEvent{Type: "run_status", Status: state})
		m.OperationID, m.RunID, m.Fence.ActionId = fmt.Sprintf("%s-ev-%d", runID, index), runID, runID
		if index == 0 {
			m.Create.ConfirmPlan = false
		} else {
			m.Create = nil
		}
		if _, err := control.AppendAgentRunEvent(m); err != nil {
			t.Fatal(err)
		}
	}
	req := agentExecutionRequest(t, control)
	req.OperationID, req.RunID, req.Fence.ActionId = runID+"-claim", runID, runID
	req.Phase, req.ExpectedIndex = "plan", 2
	claim, err := control.ClaimAgentRunExecution(req)
	if err != nil {
		t.Fatal(err)
	}
	lease := claim.Admission.Lease
	dispatchReq := AgentNodeDispatchRequest{
		OperationID: runID + "-leader-begin",
		Actor:       req.Actor,
		ActionID:    lease.ActionID,
		Epoch:       lease.Epoch,
		RunID:       req.RunID,
		NodeID:      "leader",
		Owner:       lease.Owner,
		ClaimToken:  lease.ClaimToken,
	}
	begun, err := control.BeginAgentNodeDispatch(dispatchReq)
	if err != nil {
		t.Fatal(err)
	}
	return control, dispatchReq, begun
}

func TestSuccessorPlanFailureAndCancellationSettlementAndRecoveryHistory(t *testing.T) {
	t.Run("plan_settlement", func(t *testing.T) {
		control, input, begun := planReadyFixture(t, "run_succ_plan")
		successor, lease, bound, dispatched, private := bindAndTakeOverSuccessor(t, control, input, begun, "plan", 2, true)
		protoIntent := agentOutcomeIntent(dispatched.Intent)
		source := &agentv1.AgentNodeOutcomeReceipt{
			SchemaVersion:   1,
			SignerPublicKey: successor.agentOutcomeSignerKey,
			Evidence: &agentv1.AgentNodeOutcomeEvidence{
				SchemaVersion:      1,
				Fence:              protoIntent.Fence,
				OperationId:        protoIntent.OperationId,
				RunId:              protoIntent.RunId,
				NodeId:             protoIntent.NodeId,
				Phase:              protoIntent.Phase,
				Request:            protoIntent.Request,
				PlanDigest:         protoIntent.PlanDigest,
				MetadataIndex:      protoIntent.MetadataIndex,
				MetadataEpoch:      protoIntent.MetadataEpoch,
				WriterFencingToken: lease.WriterFencingToken,
				CreatedAt:          time.Unix(protoIntent.RecordedAt, 0).UTC().Format(time.RFC3339),
				ConfirmedAt:        time.Unix(protoIntent.RecordedAt+1, 0).UTC().Format(time.RFC3339),
				OutcomeSha256:      strings.Repeat("a", 64),
				OutcomeLength:      40,
				PublicEvent:        &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 30},
				ProviderPlan:       []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}, {Id: "critic", Task: "review", DependsOn: []string{"coder"}}},
				FleetId:            successor.fleetID,
				Environment:        successor.environment,
			},
		}
		agentOutcomeSign(t, source, private)
		recorded, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
			OperationID: "plan-successor-outcome",
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			Epoch:       lease.Epoch,
			RunID:       input.RunID,
			NodeID:      "leader",
			Receipt:     source,
		})
		if err != nil {
			t.Fatal(err)
		}
		proj := &agentv1.AgentPlanProjectionReceipt{
			SchemaVersion:   1,
			SignerPublicKey: successor.agentOutcomeSignerKey,
			Evidence: &agentv1.AgentPlanProjectionEvidence{
				SchemaVersion:         1,
				SourceReceiptSha256:   recorded.ReceiptSHA256,
				SettlementFence:       &commonv1.ActionFence{ActionId: lease.ActionID, ExecutionEpoch: lease.Epoch},
				SettlementWriterToken: successor.token,
				RunId:                 input.RunID,
				MetadataEpoch:         protoIntent.MetadataEpoch,
				ExpectedIndex:         protoIntent.MetadataIndex,
				NormalizedPlan:        source.Evidence.ProviderPlan,
				NextStatus:            "running",
				PlanBody:              &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 70},
				StatusBody:            &agentv1.AgentArtifactReference{Sha256: strings.Repeat("d", 64), Length: 50},
				FleetId:               successor.fleetID,
				Environment:           successor.environment,
			},
		}
		planProjectionSign(t, proj, private)
		settled, err := successor.SettleAgentPlan(AgentPlanSettlementRequest{
			OperationID: "settle-successor-plan",
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			RunID:       input.RunID,
			Owner:       lease.Owner,
			ClaimToken:  lease.ClaimToken,
			Epoch:       lease.Epoch,
			Projection:  proj,
		})
		if err != nil || settled.Action.State != "SUCCEEDED" || settled.Run.Status != "running" {
			t.Fatalf("plan settlement failed: %+v err=%v", settled, err)
		}
		_, snapIntent, _, found, err := successor.AgentNodeOutcomeSnapshot(input.ActionID, "leader")
		if err != nil || !found || snapIntent.OperationID != bound.SuccessorOperationID || snapIntent.ExecutionEpoch != lease.Epoch {
			t.Fatalf("unexpected plan outcome snapshot: %+v found=%v err=%v", snapIntent, found, err)
		}
	})

	t.Run("failure_settlement_and_recovery_history", func(t *testing.T) {
		control, input, begun := planReadyFixture(t, "run_succ_fail")
		successor, lease, bound, dispatched, private := bindAndTakeOverSuccessor(t, control, input, begun, "plan", 2, true)
		protoIntent := agentOutcomeIntent(dispatched.Intent)
		source := &agentv1.AgentNodeOutcomeReceipt{
			SchemaVersion:   1,
			SignerPublicKey: successor.agentOutcomeSignerKey,
			Evidence: &agentv1.AgentNodeOutcomeEvidence{
				SchemaVersion:      1,
				Fence:              protoIntent.Fence,
				OperationId:        protoIntent.OperationId,
				RunId:              protoIntent.RunId,
				NodeId:             protoIntent.NodeId,
				Phase:              protoIntent.Phase,
				Request:            protoIntent.Request,
				PlanDigest:         protoIntent.PlanDigest,
				MetadataIndex:      protoIntent.MetadataIndex,
				MetadataEpoch:      protoIntent.MetadataEpoch,
				WriterFencingToken: lease.WriterFencingToken,
				CreatedAt:          time.Unix(protoIntent.RecordedAt, 0).UTC().Format(time.RFC3339),
				ConfirmedAt:        time.Unix(protoIntent.RecordedAt+1, 0).UTC().Format(time.RFC3339),
				OutcomeSha256:      strings.Repeat("a", 64),
				OutcomeLength:      112,
				PublicEvent:        &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 92},
				Failure:            &agentv1.AgentProviderFailureMetadata{SchemaVersion: 1, HttpStatus: 401, ResponseSha256: strings.Repeat("c", 64)},
				FleetId:            successor.fleetID,
				Environment:        successor.environment,
			},
		}
		agentOutcomeSign(t, source, private)
		encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(source)
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(encoded)
		sourceDigest := hex.EncodeToString(sum[:])
		if _, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
			OperationID: "fail-successor-outcome",
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			Epoch:       lease.Epoch,
			RunID:       input.RunID,
			NodeID:      "leader",
			Receipt:     source,
		}); err != nil {
			t.Fatal(err)
		}
		fp := &agentv1.AgentFailureProjectionReceipt{
			SchemaVersion:   1,
			SignerPublicKey: successor.agentOutcomeSignerKey,
			Evidence: &agentv1.AgentFailureProjectionEvidence{
				SchemaVersion:         1,
				SourceReceiptSha256:   sourceDigest,
				SettlementFence:       &commonv1.ActionFence{ActionId: lease.ActionID, ExecutionEpoch: lease.Epoch},
				SettlementWriterToken: successor.token,
				RunId:                 input.RunID,
				Phase:                 protoIntent.Phase,
				MetadataEpoch:         protoIntent.MetadataEpoch,
				ExpectedIndex:         protoIntent.MetadataIndex,
				ErrorBody:             source.Evidence.PublicEvent,
				FleetId:               successor.fleetID,
				Environment:           successor.environment,
			},
		}
		failureProjectionSign(t, fp, private)
		settled, err := successor.SettleAgentFailure(AgentFailureSettlementRequest{
			OperationID: "settle-successor-fail",
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			Epoch:       lease.Epoch,
			RunID:       input.RunID,
			Owner:       lease.Owner,
			ClaimToken:  lease.ClaimToken,
			Projection:  fp,
		})
		if err != nil || settled.Action.State != "FAILED" || settled.Run.Status != "failed" {
			t.Fatalf("failure settlement failed: %+v err=%v", settled, err)
		}
		tx, err := successor.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		history, err := successor.agentRecoveryHistoryTx(tx, settled.Run, 0)
		_ = tx.Rollback()
		if err != nil || len(history.effects) != 1 {
			t.Fatalf("recovery history failed: len=%d err=%v", len(history.effects), err)
		}
		if history.effects[0].Dispatch.OperationId != bound.SuccessorOperationID ||
			history.effects[0].Dispatch.Fence.ExecutionEpoch != lease.Epoch ||
			!proto.Equal(history.effects[0].Outcome, source) {
			t.Fatalf("recovery history lost successor provenance: %+v", history.effects[0].Dispatch)
		}
	})

	t.Run("cancellation_settlement", func(t *testing.T) {
		control, input, begun := successorReadyFixture(t)
		successor, lease, bound, dispatched, private := bindAndTakeOverSuccessor(t, control, input, begun, "tasks", 4, true)
		receipt := buildSignedTaskSuccessorReceipt(t, successor, dispatched.Intent, lease.WriterFencingToken, private)
		if _, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
			OperationID: "cancel-successor-outcome",
			Actor:       input.Actor,
			ActionID:    input.ActionID,
			Epoch:       lease.Epoch,
			RunID:       input.RunID,
			NodeID:      input.NodeID,
			Receipt:     receipt,
		}); err != nil {
			t.Fatal(err)
		}
		run, found, err := successor.AgentRunMetadata(input.RunID)
		if err != nil || !found {
			t.Fatal(err)
		}
		authority, err := successor.GetCutover("agent_run")
		if err != nil {
			t.Fatal(err)
		}
		cancelled, err := successor.AppendAgentRunEvent(AgentRunEventMutation{
			OperationID:   "cancel-successor-run",
			Actor:         input.Actor,
			RunID:         input.RunID,
			ExpectedIndex: run.NextIndex,
			Fence:         &internalprotocol.ActionFence{ActionId: input.RunID, ExecutionEpoch: uint64(authority.Epoch)},
			Event:         agent.RunEvent{Type: "run_status", Status: "cancelled", Body: agent.ArtifactRef{SHA256: strings.Repeat("d", 64), Length: 20}},
		})
		if err != nil {
			t.Fatal(err)
		}
		settled, err := successor.SettleAgentCancellation(SettleAgentCancellationRequest{
			OperationID:   "settle-cancel-successor",
			Actor:         input.Actor,
			ActionID:      input.ActionID,
			RunID:         input.RunID,
			Epoch:         lease.Epoch,
			Owner:         lease.Owner,
			ClaimToken:    lease.ClaimToken,
			ExpectedIndex: cancelled.Run.NextIndex,
		})
		if err != nil || settled.Action.State != "CANCELLED_CONFIRMED" {
			t.Fatalf("cancellation settlement failed: %+v err=%v", settled, err)
		}
		_, snapIntent, _, found, err := successor.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
		if err != nil || !found || snapIntent.OperationID != bound.SuccessorOperationID || snapIntent.ExecutionEpoch != lease.Epoch {
			t.Fatalf("cancelled outcome snapshot drifted: %+v found=%v err=%v", snapIntent, found, err)
		}
	})
}

func TestSuccessorOutcomeProvenanceValidationAndTamperRefusals(t *testing.T) {
	control, input, begun := successorReadyFixture(t)
	successor, lease, bound, dispatched, private := bindAndTakeOverSuccessor(t, control, input, begun, "tasks", 4, true)
	receipt := buildSignedTaskSuccessorReceipt(t, successor, dispatched.Intent, lease.WriterFencingToken, private)

	actionRec, found, err := successor.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatalf("missing action record: found=%v err=%v", found, err)
	}
	nodes, err := readAgentNodeDispatches(actionRec)
	if err != nil {
		t.Fatal(err)
	}
	storedIntent := nodes[input.NodeID]

	tx, err := successor.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := successor.resolveAgentOutcomeTargetTx(tx, actionRec, storedIntent, nil); !errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) {
		t.Fatalf("nil receipt returned %v, want %v", err, agentproof.ErrAgentNodeOutcomeInvalid)
	}
	if _, _, err := successor.resolveAgentOutcomeTargetTx(tx, actionRec, storedIntent, &agentv1.AgentNodeOutcomeReceipt{Evidence: &agentv1.AgentNodeOutcomeEvidence{}}); !errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) {
		t.Fatalf("missing fence returned %v, want %v", err, agentproof.ErrAgentNodeOutcomeInvalid)
	}
	unknownOp := proto.Clone(receipt).(*agentv1.AgentNodeOutcomeReceipt)
	unknownOp.Evidence.OperationId = "agent-node-" + strings.Repeat("f", 64)
	if _, _, err := successor.resolveAgentOutcomeTargetTx(tx, actionRec, storedIntent, unknownOp); !errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) {
		t.Fatalf("unknown operation id returned %v, want %v", err, agentproof.ErrAgentNodeOutcomeInvalid)
	}
	wrongAction := proto.Clone(receipt).(*agentv1.AgentNodeOutcomeReceipt)
	wrongAction.Evidence.Fence.ActionId = "other-action"
	if _, _, err := successor.resolveAgentOutcomeTargetTx(tx, actionRec, storedIntent, wrongAction); !errors.Is(err, agentproof.ErrAgentNodeOutcomeInvalid) {
		t.Fatalf("mismatched fence action returned %v, want %v", err, agentproof.ErrAgentNodeOutcomeInvalid)
	}
	if _, _, err := successor.resolveAgentOutcomeTargetTx(nil, actionRec, storedIntent, receipt); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("nil tx returned %v, want %v", err, ErrWriterFenceHeld)
	}
	_ = tx.Rollback()

	for _, tc := range []struct {
		name         string
		view         string
		query        string
		want         error
		viaTxResolve bool
	}{
		{
			name: "bad_mut_state",
			view: "control_operator_mutations",
			query: `SELECT operation_id, domain, record_id, record_revision, 'CLAIMED' AS record_state, payload_digest,
				request_digest, action_id, execution_epoch, cutover_revision, fencing_token, writer_fencing_token, actor, recorded_at
				FROM main.control_operator_mutations`,
			want: ErrAgentNodeDispatchRequired,
		},
		{
			name: "bad_payload_digest",
			view: "control_operator_mutations",
			query: `SELECT operation_id, domain, record_id, record_revision, record_state, '` + strings.Repeat("0", 64) + `' AS payload_digest,
				request_digest, action_id, execution_epoch, cutover_revision, fencing_token, writer_fencing_token, actor, recorded_at
				FROM main.control_operator_mutations`,
			want: ErrCorruptRecord,
		},
		{
			name: "bad_request_digest",
			view: "control_operator_mutations",
			query: `SELECT operation_id, domain, record_id, record_revision, record_state, payload_digest,
				'` + strings.Repeat("0", 64) + `' AS request_digest, action_id, execution_epoch, cutover_revision, fencing_token, writer_fencing_token, actor, recorded_at
				FROM main.control_operator_mutations`,
			want: ErrAgentNodeDispatchRequired,
		},
		{
			name:         "bad_dispatch_event_state",
			view:         "control_events",
			query:        `SELECT event_id, domain, record_id, revision, execution_epoch, CASE WHEN domain='action' AND state='EXECUTING' THEN 'CLAIMED' ELSE state END AS state, payload_json, record_digest, writer_fencing_token, recorded_at FROM main.control_events`,
			want:         ErrCorruptRecord,
			viaTxResolve: true,
		},
		{
			name:         "tampered_dispatch_node_json",
			view:         "control_events",
			query:        `SELECT event_id, domain, record_id, revision, execution_epoch, state, CASE WHEN domain='action' AND state='EXECUTING' THEN json_set(payload_json, '$.nativeAgentNodeDispatches.coder.task', 'tampered') ELSE payload_json END AS payload_json, record_digest, writer_fencing_token, recorded_at FROM main.control_events`,
			want:         ErrCorruptRecord,
			viaTxResolve: true,
		},
		{
			name:         "missing_takeover_lease_event",
			view:         "action_lease_events",
			query:        `SELECT event_id, action_id, event_type, owner, epoch, claim_token, lease_until, claim_revision, writer_fencing_token, recorded_at, resource_keys_json FROM main.action_lease_events WHERE NOT (event_type='TAKEOVER' AND epoch > 1)`,
			want:         ErrActionLeaseStale,
			viaTxResolve: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := successor.db.Exec("CREATE TEMP VIEW " + tc.view + " AS " + tc.query); err != nil {
				t.Fatal(err)
			}
			defer func() {
				if _, dropErr := successor.db.Exec("DROP VIEW temp." + tc.view); dropErr != nil {
					t.Fatal(dropErr)
				}
			}()
			if tc.viaTxResolve {
				rtx, err := successor.db.Begin()
				if err != nil {
					t.Fatal(err)
				}
				_, _, err = successor.resolveAgentOutcomeTargetTx(rtx, actionRec, storedIntent, receipt)
				_ = rtx.Rollback()
				if !errors.Is(err, tc.want) {
					t.Fatalf("%s returned %v, want %v", tc.name, err, tc.want)
				}
				return
			}
			_, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
				OperationID: "tamper-" + tc.name,
				Actor:       input.Actor,
				ActionID:    input.ActionID,
				Epoch:       lease.Epoch,
				RunID:       input.RunID,
				NodeID:      input.NodeID,
				Receipt:     receipt,
			})
			if !errors.Is(err, tc.want) {
				t.Fatalf("%s returned %v, want %v", tc.name, err, tc.want)
			}
		})
	}

	if _, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
		OperationID: "record-before-close-check",
		Actor:       input.Actor,
		ActionID:    input.ActionID,
		Epoch:       lease.Epoch,
		RunID:       input.RunID,
		NodeID:      input.NodeID,
		Receipt:     receipt,
	}); err != nil {
		t.Fatal(err)
	}
	updatedAction, found, err := successor.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatalf("updated action missing: found=%v err=%v", found, err)
	}
	updatedNodes, err := readAgentNodeDispatches(updatedAction)
	if err != nil {
		t.Fatal(err)
	}
	if err := successor.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := successor.agentNodeOutcomes(updatedAction, updatedNodes); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("closed store standalone reader returned %v, want %v", err, ErrCorruptRecord)
	}
	_ = bound
}

func TestSuccessorRecoveryOutputsAndProgressContext(t *testing.T) {
	control, run, private := recoveryExecutionFixture(t, "critic", true)
	signerKey := control.agentOutcomeSignerKey
	claim := recoveryExecutionClaim(t, control, run, "tasks")
	origLease := claim.Admission.Lease
	begun, err := control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{
		OperationID: "recovery-critic-begin",
		Actor:       "operator:recovery-successor",
		ActionID:    origLease.ActionID,
		Epoch:       origLease.Epoch,
		RunID:       run.RunID,
		NodeID:      "critic",
		Owner:       origLease.Owner,
		ClaimToken:  origLease.ClaimToken,
	})
	if err != nil {
		t.Fatal(err)
	}
	bound, err := control.BindAgentNodeSuccessor(AgentNodeSuccessorBindRequest{
		OperationID:       "bind-recovery-critic-successor",
		Actor:             "operator:recovery-successor",
		ActionID:          origLease.ActionID,
		Epoch:             origLease.Epoch,
		RunID:             run.RunID,
		NodeID:            "critic",
		Owner:             origLease.Owner,
		ClaimToken:        origLease.ClaimToken,
		ResetIndex:        run.NextIndex,
		Effect:            "CONFIRMED",
		ResetAdmitted:     true,
		ResetFenceMatches: true,
	})
	if err != nil {
		t.Fatal(err)
	}

	now := begun.Admission.Lease.LeaseUntil + 1
	opts := OpenOptions{
		Path:                     control.path,
		Owner:                    "recovery-successor-control",
		Now:                      func() int64 { return now },
		AuthorizeCutover:         true,
		PromotionSignerPublicKey: control.promotionSignerKey,
		FleetID:                  control.fleetID,
		Environment:              control.environment,
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = successor.Close() })
	successor.agentOutcomeSignerKey = signerKey

	expected := recoveryExpectedFromRun(t, successor, run.RunID, &agentv1.AgentRunRecoverySelection{})
	taken, err := successor.ClaimAgentRunExecution(AgentRunExecutionRequest{
		OperationID:   "takeover-recovery-tasks",
		Actor:         "operator:recovery-successor",
		RunID:         run.RunID,
		Phase:         "tasks",
		ExpectedIndex: run.NextIndex,
		Fence:         expected.Fence,
		Owner:         "recovery-successor-worker",
		LeaseSeconds:  60,
	})
	if err != nil {
		t.Fatal(err)
	}
	lease := taken.Admission.Lease
	if _, err := successor.AdmitSuccessorDispatch(AgentSuccessorEpochRequest{
		OperationID:       "admit-recovery-critic-successor",
		Actor:             "operator:recovery-successor",
		ActionID:          lease.ActionID,
		Epoch:             lease.Epoch,
		RunID:             run.RunID,
		NodeID:            "critic",
		Owner:             lease.Owner,
		ClaimToken:        lease.ClaimToken,
		Effect:            "CONFIRMED",
		ResetAdmitted:     true,
		ResetFenceMatches: true,
		ResetIndex:        bound.ResetIndex,
	}); err != nil {
		t.Fatal(err)
	}
	dispatched, err := successor.BeginAgentNodeDispatch(AgentNodeDispatchRequest{
		OperationID: bound.SuccessorOperationID,
		Actor:       "operator:recovery-successor",
		ActionID:    lease.ActionID,
		Epoch:       lease.Epoch,
		RunID:       run.RunID,
		NodeID:      "critic",
		Owner:       lease.Owner,
		ClaimToken:  lease.ClaimToken,
	})
	if err != nil {
		t.Fatal(err)
	}
	receipt := buildSignedTaskSuccessorReceipt(t, successor, dispatched.Intent, lease.WriterFencingToken, private)
	consumed, err := successor.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{
		OperationID: "record-recovery-critic-successor",
		Actor:       "operator:recovery-successor",
		ActionID:    lease.ActionID,
		Epoch:       lease.Epoch,
		RunID:       run.RunID,
		NodeID:      "critic",
		Receipt:     receipt,
	})
	if err != nil {
		t.Fatal(err)
	}
	settled, err := successor.SettleAgentTask(AgentTaskSettlementRequest{
		OperationID:         "settle-recovery-critic-successor",
		Actor:               "operator:recovery-successor",
		ActionID:            lease.ActionID,
		Epoch:               lease.Epoch,
		RunID:               run.RunID,
		NodeID:              "critic",
		Owner:               lease.Owner,
		ClaimToken:          lease.ClaimToken,
		ExpectedIndex:       run.NextIndex,
		SourceReceiptSHA256: consumed.ReceiptSHA256,
	})
	if err != nil {
		t.Fatal(err)
	}
	completed, err := successor.CompleteAgentTasks(CompleteAgentTasksRequest{
		OperationID:   "complete-recovery-critic-successor",
		Actor:         "operator:recovery-successor",
		ActionID:      lease.ActionID,
		Epoch:         lease.Epoch,
		RunID:         run.RunID,
		Owner:         lease.Owner,
		ClaimToken:    lease.ClaimToken,
		ExpectedIndex: settled.Run.NextIndex,
	})
	if err != nil {
		t.Fatal(err)
	}
	cutover, err := successor.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	progressCtx, err := successor.PreviewAgentRecoveryProgress(AgentRecoveryProgressExpected{
		RunID:           completed.Run.RunID,
		Fence:           &internalprotocol.ActionFence{ActionId: completed.Run.RunID, ExecutionEpoch: uint64(cutover.Epoch)},
		ExpectedIndex:   completed.Run.NextIndex,
		RuntimeRevision: completed.Run.RuntimeVersion(),
		Request:         completed.Run.Request,
	})
	if err != nil {
		t.Fatalf("preview recovery progress failed: %v", err)
	}
	if len(progressCtx.Outputs) != 1 || progressCtx.Outputs[0].Dispatch.OperationId != bound.SuccessorOperationID ||
		progressCtx.Outputs[0].Dispatch.Fence.ExecutionEpoch != lease.Epoch || !proto.Equal(progressCtx.Outputs[0].Outcome, receipt) {
		t.Fatalf("recovery progress context lost successor dispatch provenance: %+v", progressCtx.Outputs)
	}
}
