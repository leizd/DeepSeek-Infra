package store

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

// The recovery slice must refuse every malformed terminal settlement, range and
// forged authority boundary before it can reach a durable write.

func TestRecoveryTerminalValidationRefusesUnsettledHistory(t *testing.T) {
	control, _ := recoveryAdmissionFixture(t, "failed")
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, testCase := range []struct {
		name    string
		state   string
		payload string
		phase   string
		want    error
	}{
		{"corrupt payload", "SUCCEEDED", "{", "tasks", ErrCorruptRecord},
		{"cancel without settlement", "CANCELLED_CONFIRMED", `{}`, "tasks", ErrAgentRunRecoveryUnsettled},
		{"failed label", "FAILED", `{}`, "tasks", ErrAgentRunRecoveryUnsettled},
		{"unknown phase", "SUCCEEDED", `{}`, "bogus", ErrCorruptRecord},
		{"missing settlement", "SUCCEEDED", `{}`, "plan", ErrAgentRunRecoveryUnsettled},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			err := control.validateAgentRecoveryTerminalTx(tx, Record{State: testCase.state, Payload: []byte(testCase.payload)}, testCase.phase)
			if !errors.Is(err, testCase.want) {
				t.Fatalf("terminal validation returned %v, want %v", err, testCase.want)
			}
		})
	}
}

func TestRecoveryHistoryAndReplayRefuseInvalidRanges(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := control.agentRecoveryHistoryTx(tx, run, run.NextIndex+1); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("history accepted an out-of-range cursor: %v", err)
	}
	if _, err := recoveryReplayEventsTx(tx, run.RunID, 0, 5); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("replay accepted an empty first revision: %v", err)
	}
	if _, err := recoveryReplayEventsTx(tx, run.RunID, 1, 70); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("replay accepted a span above the retained limit: %v", err)
	}
	if _, err := recoveryReplayEventsTx(tx, run.RunID, 1, run.NextIndex+5); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("replay accepted a span longer than the retained events: %v", err)
	}
}

func TestRecoveryPreviewRefusesForgeriesBeforeReadingState(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	expected := input.AgentRunRecoveryExpected
	if _, err := control.PreviewAgentRunRecovery(AgentRunRecoveryExpected{}); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("preview accepted an empty expectation: %v", err)
	}
	fenceMismatch := expected
	fenceMismatch.Fence = &internalprotocol.ActionFence{ActionId: "another-run", ExecutionEpoch: input.Fence.ExecutionEpoch}
	if _, err := control.PreviewAgentRunRecovery(fenceMismatch); !errors.Is(err, internalprotocol.ErrFenceMismatch) {
		t.Fatalf("preview accepted a foreign action id: %v", err)
	}
	forgedEpoch := expected
	forgedEpoch.Fence = &internalprotocol.ActionFence{ActionId: input.RunID, ExecutionEpoch: input.Fence.ExecutionEpoch + 1}
	if _, err := control.PreviewAgentRunRecovery(forgedEpoch); !errors.Is(err, internalprotocol.ErrFenceMismatch) {
		t.Fatalf("preview accepted a forged execution epoch: %v", err)
	}
	unknown := expected
	unknown.RunID = "run_recovery_unknown_boundary"
	unknown.Fence = &internalprotocol.ActionFence{ActionId: unknown.RunID, ExecutionEpoch: input.Fence.ExecutionEpoch}
	if _, err := control.PreviewAgentRunRecovery(unknown); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("preview accepted an unknown run: %v", err)
	}
	control.authorizeCutover = false
	if _, err := control.PreviewAgentRunRecovery(expected); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatalf("preview ran without operator authority: %v", err)
	}
}

func TestRecoveryCommitRefusesForgedEpochAndUnknownRun(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	count := countControlEvents(t, control)
	forged := input
	forged.Fence = &internalprotocol.ActionFence{ActionId: input.RunID, ExecutionEpoch: input.Fence.ExecutionEpoch + 1}
	if _, err := control.CommitAgentRunRecovery(forged); !errors.Is(err, internalprotocol.ErrFenceMismatch) {
		t.Fatalf("commit accepted a forged execution epoch: %v", err)
	}
	if countControlEvents(t, control) != count {
		t.Fatal("forged epoch changed durable state")
	}
	unknown := input
	unknown.RunID = "run_recovery_unknown_boundary"
	unknown.Fence = &internalprotocol.ActionFence{ActionId: unknown.RunID, ExecutionEpoch: input.Fence.ExecutionEpoch}
	unknown.OperationID = "recover-unknown-run"
	if _, err := control.CommitAgentRunRecovery(unknown); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("commit accepted an unknown run: %v", err)
	}
	if countControlEvents(t, control) != count {
		t.Fatal("unknown run changed durable state")
	}
}

func TestRecoveryMetadataEdgeRefusesEveryScopeForgery(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	before, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	beforePayload, err := json.Marshal(before)
	if err != nil {
		t.Fatal(err)
	}
	beforeRecord := Record{ID: input.RunID, State: before.Status, Revision: before.NextIndex, ExecutionEpoch: before.LastEvent.ExecutionEpoch, Payload: beforePayload}
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	finalPayload, err := json.Marshal(result.Run)
	if err != nil {
		t.Fatal(err)
	}
	finalRecord := Record{ID: input.RunID, State: result.Run.Status, Revision: result.Run.NextIndex, ExecutionEpoch: result.Run.LastEvent.ExecutionEpoch, Payload: finalPayload}
	if err := validateAgentRecoveryMetadataEdge(Record{}, finalRecord); !errors.Is(err, ErrAgentRunRecoveryRequired) {
		t.Fatalf("a new record claimed a recovery scope: %v", err)
	}
	if err := validateAgentRecoveryMetadataEdge(Record{ID: input.RunID, Payload: []byte("{")}, finalRecord); err == nil {
		t.Fatal("an undecodable predecessor was accepted")
	}
	if err := validateAgentRecoveryMetadataEdge(beforeRecord, finalRecord); !errors.Is(err, ErrAgentRunRecoveryRequired) {
		t.Fatalf("a multi-event transition was accepted as one scope write: %v", err)
	}
	var firstPayload []byte
	if err := control.db.QueryRow(`SELECT payload_json FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`,
		input.RunID, before.NextIndex+1).Scan(&firstPayload); err != nil {
		t.Fatal(err)
	}
	firstRecord := Record{ID: input.RunID, State: "running", Revision: before.NextIndex + 1, ExecutionEpoch: result.Run.LastEvent.ExecutionEpoch, Payload: firstPayload}
	if err := validateAgentRecoveryMetadataEdge(beforeRecord, firstRecord); err != nil {
		t.Fatalf("the committed first scope transition was refused: %v", err)
	}
	for _, testCase := range []struct {
		name   string
		tamper func(*agent.RunMetadata) error
	}{
		{"final cursor", func(run *agent.RunMetadata) error { run.FinalAfter++; return nil }},
		{"expected request", func(run *agent.RunMetadata) error {
			run.Recovery.Metadata.ExpectedRequest = &agentv1.AgentArtifactReference{Sha256: strings.Repeat("9", 64), Length: 7}
			digest, err := agentRecoveryContextDigest(run.Recovery.Metadata)
			if err != nil {
				return err
			}
			run.Recovery.ContextSHA256 = digest
			return nil
		}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			var tampered agent.RunMetadata
			if json.Unmarshal(firstPayload, &tampered) != nil {
				t.Fatal("cannot detach the committed scope payload")
			}
			if err := testCase.tamper(&tampered); err != nil {
				t.Fatal(err)
			}
			payload, err := json.Marshal(tampered)
			if err != nil {
				t.Fatal(err)
			}
			err = validateAgentRecoveryMetadataEdge(beforeRecord, Record{ID: input.RunID, State: "running", Revision: before.NextIndex + 1, ExecutionEpoch: result.Run.LastEvent.ExecutionEpoch, Payload: payload})
			if !errors.Is(err, ErrAgentRunRecoveryRequired) {
				t.Fatalf("a tampered scope transition was accepted: %v", err)
			}
		})
	}
}

func TestRecoveryContextValidationRefusesPrivateAndMalformedInput(t *testing.T) {
	if _, err := agentRecoveryContextDigest(nil); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("digest accepted a nil context: %v", err)
	}
	unknown := &agentv1.AgentRunRecoveryContext{}
	unknown.ProtoReflect().SetUnknown([]byte{0x80, 0x06, 0x01})
	if _, err := agentRecoveryContextDigest(unknown); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("digest accepted unknown fields: %v", err)
	}
	if _, err := agentRecoveryContextDigest(&agentv1.AgentRunRecoveryContext{SchemaVersion: 1, RunId: strings.Repeat("r", 1<<20)}); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("digest accepted an oversized context: %v", err)
	}
	if _, _, err := normalizeAgentRecoverySelection(nil); !errors.Is(err, agent.ErrInvalidRunRecovery) {
		t.Fatalf("normalization accepted a nil selection: %v", err)
	}
	if _, _, err := normalizeAgentRecoverySelection(&agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME, AgentId: "coder"}); !errors.Is(err, agent.ErrInvalidRunRecovery) {
		t.Fatalf("resume accepted a selected Agent: %v", err)
	}
	if _, _, err := normalizeAgentRecoverySelection(&agentv1.AgentRunRecoverySelection{}); !errors.Is(err, agent.ErrInvalidRunRecovery) {
		t.Fatalf("normalization accepted an unknown kind: %v", err)
	}
}

func TestRecoveryScopeValidationRefusesForgedGenerations(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(result.Run)
	if err != nil {
		t.Fatal(err)
	}
	var firstGeneration agent.RunMetadata
	if json.Unmarshal(encoded, &firstGeneration) != nil {
		t.Fatal("cannot detach the committed scope")
	}
	if !validAgentRecoveryScope(firstGeneration) {
		t.Fatal("the committed first generation scope was refused")
	}
	withPrevious := agent.RunMetadata{}
	if json.Unmarshal(encoded, &withPrevious) != nil {
		t.Fatal("cannot detach the committed scope")
	}
	withPrevious.Recovery.Metadata.PreviousScope = &agentv1.AgentRunRecoveryScope{Generation: 1, StartIndex: 1}
	if validAgentRecoveryScope(withPrevious) {
		t.Fatal("a first generation scope accepted a previous scope")
	}
	secondGeneration := agent.RunMetadata{}
	if json.Unmarshal(encoded, &secondGeneration) != nil {
		t.Fatal("cannot detach the committed scope")
	}
	secondGeneration.Recovery.Generation = 2
	secondGeneration.Recovery.Metadata.Generation = 2
	if validAgentRecoveryScope(secondGeneration) {
		t.Fatal("a later generation scope accepted a missing previous scope")
	}
}

func TestRecoveryContextTransactionRefusesMalformedRecords(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "failed")
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	committed, err := json.Marshal(result.Run)
	if err != nil {
		t.Fatal(err)
	}
	expected := AgentRunRecoveryExpected{RunID: input.RunID, Fence: input.Fence, ExpectedIndex: result.Run.NextIndex,
		ExpectedRuntimeRevision: result.Run.RuntimeVersion(), ExpectedRequest: result.Run.Request,
		Selection: &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME}}
	normalized, _, err := normalizeAgentRecoverySelection(expected.Selection)
	if err != nil {
		t.Fatal(err)
	}
	expected.Selection = normalized
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := control.agentRecoveryContextTx(tx, Record{ID: input.RunID, Payload: []byte("{")}, expected); err == nil {
		t.Fatal("an undecodable record reached the recovery context")
	}
	committedRecord := Record{ID: input.RunID, State: result.Run.Status, Revision: result.Run.NextIndex, ExecutionEpoch: result.Run.LastEvent.ExecutionEpoch, Payload: committed}
	if _, err := control.agentRecoveryContextTx(tx, committedRecord, expected); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("a running scope accepted another recovery: %v", err)
	}
	badSelection := expected
	badSelection.Selection = &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME, AgentId: "coder"}
	if _, err := control.agentRecoveryContextTx(tx, committedRecord, badSelection); !errors.Is(err, agent.ErrInvalidRunRecovery) {
		t.Fatalf("the context transaction accepted a selected resume: %v", err)
	}
}
