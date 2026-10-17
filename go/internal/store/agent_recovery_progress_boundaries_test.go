package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
	"google.golang.org/protobuf/proto"
)

func progressGuardFixture(t *testing.T, synthesize bool) (*Control, agent.RunMetadata, CommitAgentRecoveryProgressRequest) {
	t.Helper()
	control, run, private := recoveryExecutionFixture(t, "critic", synthesize)
	completed := recoveryBoundaryCompleteSelection(t, control, run, private)
	return control, completed.Run, recoveryProgressInput(t, control, completed.Run, private)
}

func progressGuardClone(t *testing.T, run agent.RunMetadata) agent.RunMetadata {
	t.Helper()
	data, err := json.Marshal(run)
	if err != nil {
		t.Fatal(err)
	}
	var result agent.RunMetadata
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal(err)
	}
	return result
}

func progressGuardHistory(t *testing.T, tx *sql.Tx, runID string, revision int64) Record {
	t.Helper()
	record, _, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, runID, revision), "agent_run")
	if err != nil || !found {
		t.Fatal("history fixture is missing", err)
	}
	return record
}

func progressGuardRewriteHistory(t *testing.T, tx *sql.Tx, record Record) {
	t.Helper()
	payload, err := canonicalControlPayload(record.Payload)
	if err != nil {
		t.Fatal(err)
	}
	record.Payload = payload
	digest, err := protocol.Digest(record)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TRIGGER IF EXISTS control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`UPDATE control_events SET state=?,payload_json=?,record_digest=? WHERE domain='agent_run' AND record_id=? AND revision=?`, record.State, string(record.Payload), digest, record.ID, record.Revision); err != nil {
		t.Fatal(err)
	}
}

func TestProgressPreviewRejectsFencesAndCASWithoutWritingMetadata(t *testing.T) {
	control, _, valid := progressGuardFixture(t, false)
	before, found, err := control.Get("agent_run", valid.RunID)
	if err != nil || !found {
		t.Fatal(err)
	}
	count := countControlEvents(t, control)
	for name, change := range map[string]func(*AgentRecoveryProgressExpected){
		"invalid-run":  func(v *AgentRecoveryProgressExpected) { v.RunID = "" },
		"missing-run":  func(v *AgentRecoveryProgressExpected) { v.RunID = "missing-progress-run"; v.Fence.ActionId = v.RunID },
		"nil-fence":    func(v *AgentRecoveryProgressExpected) { v.Fence = nil },
		"wrong-fence":  func(v *AgentRecoveryProgressExpected) { v.Fence.ActionId = "another-run" },
		"stale-epoch":  func(v *AgentRecoveryProgressExpected) { v.Fence.ExecutionEpoch = 0 },
		"future-epoch": func(v *AgentRecoveryProgressExpected) { v.Fence.ExecutionEpoch++ },
		"cursor":       func(v *AgentRecoveryProgressExpected) { v.ExpectedIndex++ },
		"runtime":      func(v *AgentRecoveryProgressExpected) { v.RuntimeRevision++ },
		"request":      func(v *AgentRecoveryProgressExpected) { v.Request.SHA256 = strings.Repeat("e", 64) },
	} {
		t.Run(name, func(t *testing.T) {
			input := valid.AgentRecoveryProgressExpected
			input.Fence = proto.Clone(input.Fence).(*internalprotocol.ActionFence)
			change(&input)
			if _, err := control.PreviewAgentRecoveryProgress(input); err == nil {
				t.Fatal("invalid preview succeeded")
			}
			after, found, err := control.Get("agent_run", valid.RunID)
			if err != nil || !found || !reflect.DeepEqual(after, before) || countControlEvents(t, control) != count {
				t.Fatal("preview changed metadata/history", err)
			}
		})
	}
}

func TestProgressCannotPreviewOrCommitWhenControlIsClosed(t *testing.T) {
	control, _, input := progressGuardFixture(t, false)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.PreviewAgentRecoveryProgress(input.AgentRecoveryProgressExpected); err == nil {
		t.Fatal("closed preview succeeded")
	}
	if _, err := control.CommitAgentRecoveryProgress(input); err == nil {
		t.Fatal("closed commit succeeded")
	}
}

func TestProgressMalformedProofAndOperationRebindingRemainReadOnly(t *testing.T) {
	control, _, input := progressGuardFixture(t, false)
	bad := input
	bad.Receipt = proto.Clone(input.Receipt).(*agentv1.AgentRecoveryProgressReceipt)
	bad.Receipt.Evidence.Context.Role = "\xff"
	count := countControlEvents(t, control)
	if _, err := control.CommitAgentRecoveryProgress(bad); err == nil || countControlEvents(t, control) != count {
		t.Fatal("invalid UTF8 proof wrote progress")
	}
	result, err := control.CommitAgentRecoveryProgress(input)
	if err != nil {
		t.Fatal(err)
	}
	count = countControlEvents(t, control)
	input.Actor = "operator:another-progress-actor"
	if _, err := control.CommitAgentRecoveryProgress(input); err == nil {
		t.Fatal("operation identity was rebound")
	}
	loaded, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found || string(recoveryBoundaryRecord(t, loaded).Payload) != string(recoveryBoundaryRecord(t, result.Run).Payload) || countControlEvents(t, control) != count {
		t.Fatal("rejected replay changed the current run", err)
	}
}

func TestProgressRejectsUnreadableCorruptAndUntrustedAuthorityInputs(t *testing.T) {
	for _, failure := range []string{"missing-run-table", "missing-resource-table", "bad-record-digest", "bad-metadata-schema", "bad-task-record", "wrong-outcome-key"} {
		t.Run(failure, func(t *testing.T) {
			control, _, input := progressGuardFixture(t, false)
			count := countControlEvents(t, control)
			switch failure {
			case "missing-run-table", "missing-resource-table":
				table := "agent_runs"
				if failure == "missing-resource-table" {
					table = "action_resource_leases"
				}
				if _, err := control.db.Exec("ALTER TABLE " + table + " RENAME TO progress_guard_saved_table"); err != nil {
					t.Fatal(err)
				}
				defer func() {
					if _, err := control.db.Exec("ALTER TABLE progress_guard_saved_table RENAME TO " + table); err != nil {
						t.Error(err)
					}
				}()
			case "bad-task-record":
				if _, err := control.db.Exec("UPDATE action_journal SET record_digest='bad' WHERE id=?", input.Receipt.Evidence.Context.TaskActionId); err != nil {
					t.Fatal(err)
				}
			case "bad-record-digest":
				if _, err := control.db.Exec("UPDATE agent_runs SET record_digest='bad' WHERE id=?", input.RunID); err != nil {
					t.Fatal(err)
				}
			case "bad-metadata-schema":
				record, found, err := control.Get("agent_run", input.RunID)
				if err != nil || !found {
					t.Fatal(err)
				}
				var run agent.RunMetadata
				if err := json.Unmarshal(record.Payload, &run); err != nil {
					t.Fatal(err)
				}
				run.SchemaVersion = 99
				record.Payload, err = canonicalControlPayload(recoveryBoundaryRecord(t, run).Payload)
				if err != nil {
					t.Fatal(err)
				}
				digest, err := protocol.Digest(record)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := control.db.Exec("UPDATE agent_runs SET payload_json=?,record_digest=? WHERE id=?", string(record.Payload), digest, input.RunID); err != nil {
					t.Fatal(err)
				}
			case "wrong-outcome-key":
				control.agentOutcomeSignerKey = "invalid-deployment-public-key"
			}
			if _, err := control.PreviewAgentRecoveryProgress(input.AgentRecoveryProgressExpected); err == nil {
				t.Fatal("unverifiable progress preview succeeded")
			}
			if _, err := control.CommitAgentRecoveryProgress(input); err == nil {
				t.Fatal("unverifiable progress commit succeeded")
			}
			if countControlEvents(t, control) != count {
				t.Fatal("faulting progress wrote public history")
			}
		})
	}
}

func TestProgressOperationReceiptFailureRollsBackBothPublicEvents(t *testing.T) {
	control, run, input := progressGuardFixture(t, false)
	count := countControlEvents(t, control)
	if _, err := control.db.Exec(`CREATE TRIGGER progress_guard_receipt_failure BEFORE INSERT ON ` + operatorMutationTable + ` BEGIN SELECT RAISE(ABORT,'progress receipt unavailable'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.CommitAgentRecoveryProgress(input); err == nil {
		t.Fatal("missing operation receipt committed progress")
	}
	loaded, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found || countControlEvents(t, control) != count || string(recoveryBoundaryRecord(t, loaded).Payload) != string(recoveryBoundaryRecord(t, run).Payload) {
		t.Fatal("receipt failure left a partial event pair", err)
	}
}

func TestProgressHistoryReadRefusesMissingTamperedOrPartialSignedPrefixes(t *testing.T) {
	control, legacy, input := progressGuardFixture(t, false)
	committed, err := control.CommitAgentRecoveryProgress(input)
	if err != nil {
		t.Fatal(err)
	}
	baseline := countControlEvents(t, control)
	for _, failure := range []string{"legacy", "missing-scope", "missing-evidence", "partial-pair", "missing-base", "bad-base-digest", "invalid-base-schema", "bad-source-proof", "bad-progress-signature", "missing-second-event", "wrong-note-phase", "wrong-next-body"} {
		t.Run(failure, func(t *testing.T) {
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			run := progressGuardClone(t, committed.Run)
			context := run.RecoveryProgress.Receipt.Evidence.Context
			switch failure {
			case "legacy":
				run = legacy
			case "missing-scope":
				run.Recovery = nil
			case "missing-evidence":
				run.RecoveryProgress.Receipt.Evidence = nil
			case "partial-pair":
				record := progressGuardHistory(t, tx, run.RunID, context.ExpectedIndex+1)
				run, err = decodeAgentMetadata(record)
				if err != nil {
					t.Fatal(err)
				}
			case "missing-base", "missing-second-event":
				if _, err := tx.Exec("DROP TRIGGER IF EXISTS control_events_no_delete"); err != nil {
					t.Fatal(err)
				}
				revision := context.ExpectedIndex
				if failure == "missing-second-event" {
					revision += 2
				}
				if _, err := tx.Exec("DELETE FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?", run.RunID, revision); err != nil {
					t.Fatal(err)
				}
			case "bad-base-digest":
				if _, err := tx.Exec("DROP TRIGGER control_events_no_update"); err != nil {
					t.Fatal(err)
				}
				if _, err := tx.Exec("UPDATE control_events SET record_digest='bad' WHERE domain='agent_run' AND record_id=? AND revision=?", run.RunID, context.ExpectedIndex); err != nil {
					t.Fatal(err)
				}
			case "invalid-base-schema", "bad-source-proof":
				record := progressGuardHistory(t, tx, run.RunID, context.ExpectedIndex)
				previous, err := decodeAgentMetadata(record)
				if err != nil {
					t.Fatal(err)
				}
				if failure == "invalid-base-schema" {
					previous.SchemaVersion = 99
				} else {
					previous.Recovery.Metadata.RetainedOutputs[0].Outcome.Signature[0] ^= 1
					digest, err := agentRecoveryContextDigest(previous.Recovery.Metadata)
					if err != nil {
						t.Fatal(err)
					}
					previous.Recovery.ContextSHA256 = digest
				}
				record.Payload = recoveryBoundaryRecord(t, previous).Payload
				progressGuardRewriteHistory(t, tx, record)
			case "bad-progress-signature":
				run.RecoveryProgress.Receipt.Signature[0] ^= 1
			case "wrong-note-phase", "wrong-next-body":
				revision := context.ExpectedIndex + 1
				if failure == "wrong-next-body" {
					revision++
				}
				record := progressGuardHistory(t, tx, run.RunID, revision)
				previous, err := decodeAgentMetadata(record)
				if err != nil {
					t.Fatal(err)
				}
				if failure == "wrong-note-phase" {
					previous.LastEvent.Phase = "researcher"
				} else {
					previous.LastEvent.Body.SHA256 = strings.Repeat("e", 64)
				}
				record.Payload = recoveryBoundaryRecord(t, previous).Payload
				progressGuardRewriteHistory(t, tx, record)
			}
			err = control.validateRecoveryProgressTx(tx, run)
			if failure == "legacy" {
				if err != nil {
					t.Fatal("legacy metadata acquired a progress requirement", err)
				}
			} else if err == nil {
				t.Fatal("unverified history was accepted", failure)
			}
		})
		if countControlEvents(t, control) != baseline {
			t.Fatal("rolled-back history fault persisted")
		}
		if _, found, err := control.AgentRunMetadata(input.RunID); err != nil || !found {
			t.Fatal("history fault damaged the valid run", err)
		}
	}
}

func TestProgressReplayDoesNotReconstructDeletedOriginalEvents(t *testing.T) {
	control, _, input := progressGuardFixture(t, false)
	if _, err := control.CommitAgentRecoveryProgress(input); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER IF EXISTS control_events_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DELETE FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?", input.RunID, input.ExpectedIndex+2); err != nil {
		t.Fatal(err)
	}
	count := countControlEvents(t, control)
	if _, err := control.CommitAgentRecoveryProgress(input); err == nil || countControlEvents(t, control) != count {
		t.Fatal("replay invented missing signed history")
	}
}

func TestOrdinaryMetadataCannotAddReplaceOrRemoveRecoveryProgress(t *testing.T) {
	for _, change := range []string{"add", "replace", "remove"} {
		t.Run(change, func(t *testing.T) {
			control, run, input := progressGuardFixture(t, false)
			if change != "add" {
				result, err := control.CommitAgentRecoveryProgress(input)
				if err != nil {
					t.Fatal(err)
				}
				run = result.Run
			}
			next := progressGuardClone(t, run)
			event := agent.RunEvent{RunID: run.RunID, ActionID: run.RunID, ExecutionEpoch: input.Fence.ExecutionEpoch, Index: run.NextIndex, Type: "agent_note", Phase: "critic", CreatedAt: run.LastEvent.CreatedAt, Body: agent.ArtifactRef{SHA256: strings.Repeat("c", 64), Length: 80}}
			next.ApplyRunEvent(event)
			switch change {
			case "add":
				next.SchemaVersion = 4
				next.RecoveryProgress = &agent.RunRecoveryProgress{Receipt: proto.Clone(input.Receipt).(*agentv1.AgentRecoveryProgressReceipt)}
			case "replace":
				next.RecoveryProgress.Receipt.Signature[0] ^= 1
			case "remove":
				next.SchemaVersion = 3
				next.RecoveryProgress = nil
			}
			write, err := prepareControlRecordWrite(recoveryBoundaryRecord(t, next), nil)
			if err != nil {
				t.Fatal(err)
			}
			write.agentEvent = true
			count := countControlEvents(t, control)
			control.mu.Lock()
			tx, authority, err := control.beginOperatorMutationTx("progress-ordinary-metadata", run.RunID, "operator:progress-guard", "agent_run")
			if err != nil {
				control.mu.Unlock()
				t.Fatal(err)
			}
			err = control.putControlRecordTx(tx, write, authority.now)
			tx.Rollback()
			control.mu.Unlock()
			if !errors.Is(err, ErrAgentRunRecoveryRequired) || countControlEvents(t, control) != count {
				t.Fatal("ordinary event changed the signed progress marker", change, err)
			}
		})
	}
}

func TestProgressResourceCatalogReadFailureCannotMeanUnreserved(t *testing.T) {
	control, run, _ := progressGuardFixture(t, false)
	count := countControlEvents(t, control)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE action_resource_leases"); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	err = agentRecoveryProgressUnreservedTx(tx, run.RunID)
	tx.Rollback()
	if err == nil || countControlEvents(t, control) != count {
		t.Fatal("an unavailable resource catalog was treated as empty")
	}
}

func TestProgressMetadataEdgesRequireTheExactNextSignedEvent(t *testing.T) {
	control, before, input := progressGuardFixture(t, false)
	committed, err := control.CommitAgentRecoveryProgress(input)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	firstRecord := progressGuardHistory(t, tx, before.RunID, input.ExpectedIndex+1)
	first, err := decodeAgentMetadata(firstRecord)
	tx.Rollback()
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range []string{"skip-next", "replace-proof", "remove-proof", "wrong-cursor"} {
		t.Run(change, func(t *testing.T) {
			previous, next := progressGuardClone(t, first), progressGuardClone(t, first)
			switch change {
			case "skip-next":
				event := first.LastEvent
				event.Index = first.NextIndex
				next.ApplyRunEvent(event)
			case "replace-proof":
				previous = progressGuardClone(t, committed.Run)
				next = progressGuardClone(t, committed.Run)
				next.RecoveryProgress.Receipt.Signature[0] ^= 1
			case "remove-proof":
				next.SchemaVersion = 3
				next.RecoveryProgress = nil
			case "wrong-cursor":
				previous = before
				next.NextIndex++
				next.LastEvent.Index++
			}
			if err := validateRecoveryProgressEdge(recoveryBoundaryRecord(t, previous), recoveryBoundaryRecord(t, next), previous, next); !errors.Is(err, ErrAgentRunRecoveryRequired) {
				t.Fatal("unsigned progress sequence was accepted", change, err)
			}
		})
	}
	if !changedRecoveryProgress(Record{Payload: json.RawMessage("{")}, firstRecord) || !changedRecoveryProgress(firstRecord, Record{Payload: json.RawMessage("{")}) {
		t.Fatal("corrupt JSON was treated as an unchanged proof")
	}
	if recoveryTaskProgressRequired(agent.RunMetadata{}) {
		t.Fatal("legacy metadata invented a selected recovery task")
	}
	context := first.RecoveryProgress.Receipt.Evidence.Context
	if matchesRecoveryProgressEvent(first.LastEvent, nil, first.RecoveryProgress.Receipt.Evidence.NoteBody, true) || matchesRecoveryProgressEvent(first.LastEvent, context, nil, true) {
		t.Fatal("missing signed event binding was accepted")
	}
}

func TestCentralProgressWriterRechecksHistoryAndSignatureInsteadOfTrustingItsFlag(t *testing.T) {
	for _, fault := range []string{"signature", "previous-schema", "next-schema", "source-key", "resource-catalog"} {
		t.Run(fault, func(t *testing.T) {
			control, run, input := progressGuardFixture(t, false)
			count := countControlEvents(t, control)
			next := progressGuardClone(t, run)
			next.SchemaVersion = 4
			next.RecoveryProgress = &agent.RunRecoveryProgress{Receipt: proto.Clone(input.Receipt).(*agentv1.AgentRecoveryProgressReceipt)}
			next.ApplyRunEvent(agent.RunEvent{RunID: run.RunID, ActionID: run.RunID, ExecutionEpoch: input.Fence.ExecutionEpoch, Index: run.NextIndex,
				Type: "agent_note", Phase: "critic", CreatedAt: run.LastEvent.CreatedAt, Body: agent.ArtifactRef{SHA256: input.Receipt.Evidence.NoteBody.Sha256, Length: input.Receipt.Evidence.NoteBody.Length}})
			if fault == "signature" {
				next.RecoveryProgress.Receipt.Signature[0] ^= 1
			}
			if fault == "next-schema" {
				next.SchemaVersion = 99
			}
			write, err := prepareControlRecordWrite(recoveryBoundaryRecord(t, next), nil)
			if err != nil {
				t.Fatal(err)
			}
			write.agentEvent, write.agentRecoveryProgress = true, true
			control.mu.Lock()
			tx, authority, err := control.beginOperatorMutationTx("progress-central-guard", run.RunID, "operator:progress-guard", "agent_run")
			if err != nil {
				control.mu.Unlock()
				t.Fatal(err)
			}
			// This transaction is isolated to the fixture and always rolled back.
			originalKey := control.agentOutcomeSignerKey
			if fault == "source-key" {
				control.agentOutcomeSignerKey = "wrong-deployment-public-key"
			}
			if fault == "previous-schema" {
				previous, found, readErr := readAgentRecordTx(tx, run.RunID)
				if readErr != nil || !found {
					tx.Rollback()
					control.mu.Unlock()
					t.Fatal(readErr)
				}
				bad := progressGuardClone(t, run)
				bad.SchemaVersion = 99
				previous.Payload, err = canonicalControlPayload(recoveryBoundaryRecord(t, bad).Payload)
				if err != nil {
					tx.Rollback()
					control.mu.Unlock()
					t.Fatal(err)
				}
				digest, digestErr := protocol.Digest(previous)
				if digestErr != nil {
					tx.Rollback()
					control.mu.Unlock()
					t.Fatal(digestErr)
				}
				if _, err := tx.Exec("UPDATE agent_runs SET payload_json=?,record_digest=? WHERE id=?", string(previous.Payload), digest, run.RunID); err != nil {
					tx.Rollback()
					control.mu.Unlock()
					t.Fatal(err)
				}
			}
			if fault == "resource-catalog" {
				if _, err := tx.Exec("DROP TABLE action_resource_leases"); err != nil {
					tx.Rollback()
					control.mu.Unlock()
					t.Fatal(err)
				}
			}
			err = control.putControlRecordTx(tx, write, authority.now)
			control.agentOutcomeSignerKey = originalKey
			tx.Rollback()
			control.mu.Unlock()
			if err == nil || countControlEvents(t, control) != count {
				t.Fatal("the private writer flag bypassed evidence verification", fault, err)
			}
			loaded, found, readErr := control.AgentRunMetadata(run.RunID)
			if readErr != nil || !found || string(recoveryBoundaryRecord(t, loaded).Payload) != string(recoveryBoundaryRecord(t, run).Payload) {
				t.Fatal("rejected central write changed authoritative metadata", readErr)
			}
		})
	}
}
