package store

import (
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func recoveryProgressInput(t *testing.T, control *Control, run agent.RunMetadata, private ed25519.PrivateKey) CommitAgentRecoveryProgressRequest {
	t.Helper()
	expected := recoveryExpectedFromRun(t, control, run.RunID, &agentv1.AgentRunRecoverySelection{})
	input := CommitAgentRecoveryProgressRequest{AgentRecoveryProgressExpected: AgentRecoveryProgressExpected{RunID: run.RunID,
		Fence: expected.Fence, ExpectedIndex: run.NextIndex, RuntimeRevision: run.RuntimeVersion(), Request: run.Request},
		OperationID: "recovery-task-progress", Actor: "operator:recovery-progress"}
	context, err := control.PreviewAgentRecoveryProgress(input.AgentRecoveryProgressExpected)
	if err != nil {
		t.Fatal(err)
	}
	input.Receipt = &agentv1.AgentRecoveryProgressReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey,
		Evidence: &agentv1.AgentRecoveryProgressEvidence{SchemaVersion: 1, Context: context, FleetId: control.fleetID, Environment: control.environment,
			NoteBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 80},
			NextBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("d", 64), Length: 40}}}
	proof, err := (proto.MarshalOptions{Deterministic: true}).Marshal(input.Receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	input.Receipt.Signature = ed25519.Sign(private, append([]byte(agentproof.RecoveryProgressDomain), proof...))
	return input
}

func recoveryProgressActionSnapshot(t *testing.T, control *Control) string {
	t.Helper()
	records, err := control.ListAuthoritativeRecords("action")
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(records)
	if err != nil {
		t.Fatal(err)
	}
	return string(encoded)
}

func TestRecoveryProgressCommitsNoteAndLateResetOrDoneWithoutNewEffect(t *testing.T) {
	for _, synthesize := range []bool{true, false} {
		t.Run(map[bool]string{true: "resynthesize", false: "preserve-final"}[synthesize], func(t *testing.T) {
			control, run, private := recoveryExecutionFixture(t, "critic", synthesize)
			completed := recoveryBoundaryCompleteSelection(t, control, run, private)
			input := recoveryProgressInput(t, control, completed.Run, private)
			context := input.Receipt.Evidence.Context
			if context.TaskActionId != completed.Action.ID || context.TaskActionRevision != completed.Action.Revision || len(context.Outputs) != 1 ||
				context.Outputs[0].Dispatch.NodeId != "critic" || context.Outputs[0].Outcome == nil || context.Outputs[0].SettlementMetadataIndex >= completed.Run.NextIndex {
				t.Fatal("progress replaced the independently completed original task")
			}
			actions, count := recoveryProgressActionSnapshot(t, control), countControlEvents(t, control)
			result, err := control.CommitAgentRecoveryProgress(input)
			if err != nil {
				t.Fatal(err)
			}
			if result.Run.SchemaVersion != 4 || len(result.Events) != 2 || result.Events[0].Type != "agent_note" || result.Events[0].Phase != "critic" ||
				result.Run.NextIndex != completed.Run.NextIndex+2 || countControlEvents(t, control) != count+2 ||
				result.Run.Request != completed.Run.Request || result.Run.RuntimeVersion() != completed.Run.RuntimeVersion() ||
				result.Run.RecoveryProgress == nil || !proto.Equal(result.Run.RecoveryProgress.Receipt, input.Receipt) ||
				recoveryProgressActionSnapshot(t, control) != actions {
				t.Fatal("public progress changed effect authority or committed partially")
			}
			if synthesize {
				if result.Events[1].Type != "final_reset" || result.Events[1].Scope != "final_answer" || result.Run.Status != "running" || result.Run.FinalAfter != completed.Run.NextIndex+1 {
					t.Fatal("final answer was not reset after selected task termination")
				}
				claim := recoveryExecutionClaim(t, control, result.Run, "synthesis")
				if claim.Admission.Record.State != "CLAIMED" {
					t.Fatal("verified late reset did not admit a separate synthesis")
				}
			} else if result.Events[1].Type != "run_status" || result.Events[1].Status != "done" || result.Run.Status != "done" || result.Run.FinalAfter != completed.Run.FinalAfter {
				t.Fatal("rerun without synthesis changed the retained final answer")
			}
			loaded, found, err := control.AgentRunMetadata(run.RunID)
			if err != nil || !found || !reflect.DeepEqual(loaded, result.Run) {
				t.Fatal("committed progress failed the immutable-history read", err)
			}
			for _, downgrade := range []bool{true, false} {
				copy := result.Run
				if downgrade {
					copy.SchemaVersion = 3
				} else {
					copy.RecoveryProgress = nil
				}
				if _, err := decodeAgentMetadata(recoveryBoundaryRecord(t, copy)); err == nil {
					t.Fatal("progress metadata accepted a lossy schema downgrade or missing receipt")
				}
			}
			beforeReplay := countControlEvents(t, control)
			replay, err := control.CommitAgentRecoveryProgress(input)
			if err != nil || replay.Mutation.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(replay.Events, result.Events) ||
				!reflect.DeepEqual(replay.Run, loaded) || countControlEvents(t, control) != beforeReplay {
				t.Fatal("retry changed event identity or created another transition", err)
			}
			if !synthesize {
				expected := recoveryExpectedFromRun(t, control, run.RunID, &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN,
					AgentId: "coder", Resynthesize: proto.Bool(false)})
				next, _ := recoveryInputFromPreview(t, control, expected)
				next.OperationID = "recovery-next-generation"
				next.Request.SHA256, next.Context.SHA256 = strings.Repeat("1", 64), strings.Repeat("2", 64)
				following, err := control.CommitAgentRunRecovery(next)
				if err != nil || following.Run.Recovery.Generation != run.Recovery.Generation+1 || following.Run.RecoveryProgress != nil {
					t.Fatal("next generation retained a stale progress marker", err)
				}
				replay, err := control.CommitAgentRecoveryProgress(input)
				if err != nil || !reflect.DeepEqual(replay.Events, result.Events) || !reflect.DeepEqual(replay.Run, following.Run) {
					t.Fatal("old operation replay replaced the current generation", err)
				}
			}
		})
	}
}

func TestRecoveryProgressRejectsUnterminatedTaskStaleScopeAndInvalidProof(t *testing.T) {
	control, run, private := recoveryExecutionFixture(t, "critic", true)
	expected := recoveryExpectedFromRun(t, control, run.RunID, &agentv1.AgentRunRecoverySelection{})
	input := AgentRecoveryProgressExpected{RunID: run.RunID, Fence: expected.Fence, ExpectedIndex: run.NextIndex, RuntimeRevision: run.RuntimeVersion(), Request: run.Request}
	if _, err := control.PreviewAgentRecoveryProgress(input); err == nil {
		t.Fatal("uncompleted selected task became metadata authority")
	}
	settled := recoveryBoundaryCompleteSelection(t, control, run, private, true)
	input.ExpectedIndex = settled.Run.NextIndex
	if _, err := control.PreviewAgentRecoveryProgress(input); err == nil {
		t.Fatal("public success flag replaced independent phase termination")
	}
}

func TestRecoveryProgressInvalidRequestsDoNotChangeAnyDurableState(t *testing.T) {
	control, run, private := recoveryExecutionFixture(t, "critic", true)
	completed := recoveryBoundaryCompleteSelection(t, control, run, private)
	valid := recoveryProgressInput(t, control, completed.Run, private)
	count, actions := countControlEvents(t, control), recoveryProgressActionSnapshot(t, control)
	for name, change := range map[string]func(*CommitAgentRecoveryProgressRequest){
		"run":           func(i *CommitAgentRecoveryProgressRequest) { i.RunID = "" },
		"missing-run":   func(i *CommitAgentRecoveryProgressRequest) { i.RunID = "missing-run"; i.Fence.ActionId = i.RunID },
		"cursor":        func(i *CommitAgentRecoveryProgressRequest) { i.ExpectedIndex++ },
		"runtime":       func(i *CommitAgentRecoveryProgressRequest) { i.RuntimeRevision++ },
		"request":       func(i *CommitAgentRecoveryProgressRequest) { i.Request.SHA256 = strings.Repeat("e", 64) },
		"no-fence":      func(i *CommitAgentRecoveryProgressRequest) { i.Fence = nil },
		"wrong-fence":   func(i *CommitAgentRecoveryProgressRequest) { i.Fence.ActionId = "another-run" },
		"epoch":         func(i *CommitAgentRecoveryProgressRequest) { i.Fence.ExecutionEpoch++ },
		"no-proof":      func(i *CommitAgentRecoveryProgressRequest) { i.Receipt = nil },
		"bad-signature": func(i *CommitAgentRecoveryProgressRequest) { i.Receipt.Signature[0] ^= 1 },
		"nested-unknown": func(i *CommitAgentRecoveryProgressRequest) {
			i.Receipt.Evidence.Context.Outputs[0].Outcome.Evidence.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
		},
		"old-signed-cursor": func(i *CommitAgentRecoveryProgressRequest) { i.Receipt.Evidence.Context.ExpectedIndex-- },
		"oversized": func(i *CommitAgentRecoveryProgressRequest) {
			i.Receipt.SignerPublicKey = strings.Repeat("a", (1<<20)+1)
		},
	} {
		t.Run(name, func(t *testing.T) {
			input := valid
			input.Fence = proto.Clone(valid.Fence).(*internalprotocol.ActionFence)
			input.Receipt = proto.Clone(valid.Receipt).(*agentv1.AgentRecoveryProgressReceipt)
			change(&input)
			if _, err := control.CommitAgentRecoveryProgress(input); err == nil {
				t.Fatal("invalid progress was accepted")
			}
			if countControlEvents(t, control) != count || recoveryProgressActionSnapshot(t, control) != actions {
				t.Fatal("rejected progress changed durable state")
			}
		})
	}
}

func TestRecoveryProgressCompareAndSwapAndSecondEventRollback(t *testing.T) {
	control, run, private := recoveryExecutionFixture(t, "critic", false)
	completed := recoveryBoundaryCompleteSelection(t, control, run, private)
	input := recoveryProgressInput(t, control, completed.Run, private)
	count := countControlEvents(t, control)
	if _, err := control.db.Exec(`CREATE TRIGGER reject_recovery_progress_done BEFORE INSERT ON control_events WHEN NEW.domain='agent_run' AND NEW.state='done' BEGIN SELECT RAISE(ABORT,'injected progress write failure'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.CommitAgentRecoveryProgress(input); err == nil || countControlEvents(t, control) != count {
		t.Fatal("second event failure retained a partial note")
	}
	if _, err := control.db.Exec("DROP TRIGGER reject_recovery_progress_done"); err != nil {
		t.Fatal(err)
	}
	var wait sync.WaitGroup
	results := make(chan error, 2)
	for _, operation := range []string{"progress-concurrent-a", "progress-concurrent-b"} {
		wait.Add(1)
		go func(operation string) {
			defer wait.Done()
			copy := input
			copy.OperationID = operation
			_, err := control.CommitAgentRecoveryProgress(copy)
			results <- err
		}(operation)
	}
	wait.Wait()
	close(results)
	applied, rejected := 0, 0
	for err := range results {
		if err == nil {
			applied++
		} else if errors.Is(err, ErrRevisionConflict) {
			rejected++
		} else {
			t.Fatal(err)
		}
	}
	if applied != 1 || rejected != 1 || countControlEvents(t, control) != count+2 {
		t.Fatal("concurrent progress did not commit exactly one event pair", applied, rejected)
	}
}
