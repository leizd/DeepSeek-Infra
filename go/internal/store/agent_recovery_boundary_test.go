package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func recoveryBoundaryExec(t *testing.T, control *Control, statements ...string) {
	t.Helper()
	for _, statement := range statements {
		if _, err := control.db.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
}

func recoveryBoundaryRecord(t *testing.T, run agent.RunMetadata) Record {
	t.Helper()
	payload, err := json.Marshal(run)
	if err != nil {
		t.Fatal(err)
	}
	return Record{Domain: "agent_run", ID: run.RunID, Revision: run.NextIndex, State: run.Status,
		ExecutionEpoch: run.LastEvent.ExecutionEpoch, Payload: payload}
}

func TestRecoveryPublicAdmissionRejectsInvalidSelectionsFencesAndCASWithoutWrites(t *testing.T) {
	for _, change := range []string{"resume_role", "unknown_selection", "unknown_role", "nil_fence", "different_fence", "stale_epoch", "overflow_epoch", "missing_run",
		"invalid_id", "cursor", "runtime", "request", "missing_operation", "invalid_actor", "invalid_event", "same_request"} {
		t.Run(change, func(t *testing.T) {
			control, input := recoveryAdmissionFixture(t, "cancelled")
			before, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil {
				t.Fatal(err)
			}
			count, originalID := countControlEvents(t, control), input.RunID
			preview := true
			switch change {
			case "resume_role":
				input.Selection.AgentId = "coder"
			case "unknown_selection":
				input.Selection.Kind = agentv1.AgentRunRecoveryKind(99)
			case "unknown_role":
				input.Selection.Kind, input.Selection.AgentId = agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN, "unregistered-role"
			case "nil_fence":
				input.Fence = nil
			case "different_fence":
				input.Fence.ActionId = "another-run"
			case "stale_epoch":
				input.Fence.ExecutionEpoch++
			case "overflow_epoch":
				input.Fence.ExecutionEpoch = math.MaxUint64
			case "missing_run":
				input.RunID, input.Fence.ActionId = "absent-recovery-run", "absent-recovery-run"
			case "invalid_id":
				input.RunID = "../outside"
			case "cursor":
				input.ExpectedIndex++
			case "runtime":
				input.ExpectedRuntimeRevision++
			case "request":
				input.ExpectedRequest.SHA256 = strings.Repeat("7", 64)
			case "missing_operation":
				preview, input.OperationID = false, ""
			case "invalid_actor":
				preview, input.Actor = false, ""
			case "invalid_event":
				preview, input.EventBodies[0].Length = false, 0
			case "same_request":
				preview, input.Request = false, input.ExpectedRequest
			}
			if preview {
				if _, err := control.PreviewAgentRunRecovery(input.AgentRunRecoveryExpected); err == nil {
					t.Fatal("invalid recovery acquired a custody preparation context")
				}
			}
			if _, err := control.CommitAgentRunRecovery(input); err == nil {
				t.Fatal("invalid recovery committed a fresh generation")
			}
			after, _, err := control.AgentRunMetadata(originalID)
			if err != nil || !reflect.DeepEqual(before, after) || countControlEvents(t, control) != count {
				t.Fatal("rejected admission changed the request, cancellation or public history", err)
			}
		})
	}
}

func TestRecoveryReceiptAndCommitFaultsRollBackTheWholeGeneration(t *testing.T) {
	for _, fault := range []string{"receipt_insert", "writer_changed", "deferred_commit"} {
		t.Run(fault, func(t *testing.T) {
			control, input := recoveryAdmissionFixture(t, "cancelled")
			before, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil {
				t.Fatal(err)
			}
			count, originalToken, originalNow := countControlEvents(t, control), control.token, control.now
			switch fault {
			case "receipt_insert":
				recoveryBoundaryExec(t, control, `CREATE TEMP TRIGGER recovery_receipt_fault BEFORE INSERT ON control_operator_mutations
                    BEGIN SELECT RAISE(ABORT,'injected recovery receipt failure'); END`)
			case "writer_changed":
				now, calls := control.now(), 0
				control.now = func() int64 {
					calls++
					if calls == 2 {
						control.token++
					}
					return now
				}
			case "deferred_commit":
				recoveryBoundaryExec(t, control,
					"CREATE TEMP TABLE recovery_parent(id INTEGER PRIMARY KEY)",
					"CREATE TEMP TABLE recovery_child(id INTEGER REFERENCES recovery_parent(id) DEFERRABLE INITIALLY DEFERRED)",
					"CREATE TEMP TRIGGER recovery_receipt_fault AFTER INSERT ON control_operator_mutations BEGIN INSERT INTO recovery_child VALUES(1); END")
			}
			_, rejected := control.CommitAgentRunRecovery(input)
			control.token, control.now = originalToken, originalNow
			if rejected == nil {
				t.Fatal("failed authority transaction reported a committed generation")
			}
			after, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil || !reflect.DeepEqual(before, after) || countControlEvents(t, control) != count {
				t.Fatal("failed receipt/commit left a partial recovery", err)
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatal("failed recovery acquired a replayable receipt", err)
			}
			if fault != "writer_changed" {
				recoveryBoundaryExec(t, control, "DROP TRIGGER recovery_receipt_fault")
			}
			if _, err := control.CommitAgentRunRecovery(input); err != nil {
				t.Fatal("rolled-back operation could not be retried", err)
			}
		})
	}
}

func TestRecoveryReplayStillRequiresTheLiveWriter(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	first, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	count, originalToken, originalNow := countControlEvents(t, control), control.token, control.now
	now, calls := control.now(), 0
	control.now = func() int64 {
		calls++
		if calls == 2 {
			control.token++
		}
		return now
	}
	_, rejected := control.CommitAgentRunRecovery(input)
	control.token, control.now = originalToken, originalNow
	if rejected == nil || countControlEvents(t, control) != count {
		t.Fatal("stale writer obtained a successful replay", rejected)
	}
	replayed, err := control.CommitAgentRunRecovery(input)
	if err != nil || replayed.Mutation.Status != OperatorMutationAlreadyApplied || replayed.Run.NextIndex != first.Run.NextIndex {
		t.Fatal("writer failure changed the original receipt", err)
	}
}

func TestRecoveryReplayRejectsReceiptsForAnotherRunOrAnIncompleteCursor(t *testing.T) {
	for _, fault := range []string{"run", "cursor"} {
		t.Run(fault, func(t *testing.T) {
			control, input := recoveryAdmissionFixture(t, "cancelled")
			first, err := control.CommitAgentRunRecovery(input)
			if err != nil {
				t.Fatal(err)
			}
			count := countControlEvents(t, control)
			id, cursor := "record_id", "record_revision"
			if fault == "run" {
				id = "'another-run'"
			} else {
				cursor = fmt.Sprintf("%d", input.ExpectedIndex)
			}
			recoveryBoundaryExec(t, control, fmt.Sprintf(`CREATE TEMP VIEW control_operator_mutations AS
                SELECT operation_id,domain,%s AS record_id,%s AS record_revision,record_state,payload_digest,
                request_digest,action_id,execution_epoch,cutover_revision,fencing_token,writer_fencing_token,actor,recorded_at
                FROM main.control_operator_mutations`, id, cursor))
			_, rejected := control.CommitAgentRunRecovery(input)
			recoveryBoundaryExec(t, control, "DROP VIEW temp.control_operator_mutations")
			if !errors.Is(rejected, ErrCorruptRecord) || countControlEvents(t, control) != count {
				t.Fatal("corrupt operator receipt was accepted as a complete recovery replay", rejected)
			}
			replayed, err := control.CommitAgentRunRecovery(input)
			if err != nil || replayed.Run.NextIndex != first.Run.NextIndex || replayed.Mutation.Status != OperatorMutationAlreadyApplied {
				t.Fatal("rejected replay changed the original operation", err)
			}
		})
	}
}

func TestRecoveryCannotSkipOrRebindTheFirstGenerationEdge(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	previous, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	var payload string
	if err := control.db.QueryRow(`SELECT payload_json FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`,
		input.RunID, input.ExpectedIndex+1).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	var first agent.RunMetadata
	if json.Unmarshal([]byte(payload), &first) != nil {
		t.Fatal("missing first generation snapshot")
	}
	before, after := recoveryBoundaryRecord(t, previous), recoveryBoundaryRecord(t, first)
	if err := validateAgentRecoveryMetadataEdge(before, after); err != nil {
		t.Fatal("fixture is not an accepted generation edge", err)
	}
	for _, change := range []string{"create_with_scope", "corrupt_predecessor", "skipped_headers", "rebound_request", "changed_plan"} {
		t.Run(change, func(t *testing.T) {
			left, right := before, after
			var next agent.RunMetadata
			if json.Unmarshal(after.Payload, &next) != nil {
				t.Fatal("cannot detach accepted fixture")
			}
			switch change {
			case "create_with_scope":
				left = Record{}
			case "corrupt_predecessor":
				left.Payload = json.RawMessage(`{}`)
			case "skipped_headers":
				right = recoveryBoundaryRecord(t, result.Run)
			case "rebound_request":
				next.Recovery.Metadata.ExpectedRequest.Sha256 = strings.Repeat("7", 64)
				next.Recovery.ContextSHA256, err = agentRecoveryContextDigest(next.Recovery.Metadata)
				if err != nil {
					t.Fatal(err)
				}
				right = recoveryBoundaryRecord(t, next)
			case "changed_plan":
				next.Plan[0].Task = "replace the retained task"
				right = recoveryBoundaryRecord(t, next)
			}
			if err := validateAgentRecoveryMetadataEdge(left, right); err == nil {
				t.Fatal("invalid predecessor or extra mutation was accepted")
			}
		})
	}
	if !changedAgentRecoveryScope(Record{Payload: json.RawMessage(`{`)}, after) ||
		!changedAgentRecoveryScope(before, Record{Payload: json.RawMessage(`{`)}) {
		t.Fatal("unreadable scope was treated as unchanged")
	}
}

func TestRecoveryScopeRejectsUnsupportedLineageAndContextSize(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	result, err := control.CommitAgentRunRecovery(input)
	if err != nil {
		t.Fatal(err)
	}
	for _, generation := range []uint64{1, 2} {
		run := result.Run
		scope := *run.Recovery
		scope.Metadata = proto.Clone(scope.Metadata).(*agentv1.AgentRunRecoveryContext)
		run.Recovery, scope.Generation, scope.Metadata.Generation = &scope, generation, generation
		if generation == 1 {
			scope.Metadata.PreviousScope = &agentv1.AgentRunRecoveryScope{Generation: 1}
		} else {
			scope.Metadata.PreviousScope = nil
		}
		if validAgentRecoveryScope(run) {
			t.Fatal("unsupported recovery lineage became readable")
		}
	}
	for _, context := range []*agentv1.AgentRunRecoveryContext{nil, {},
		{SchemaVersion: 1, RunId: strings.Repeat("x", (1<<20)+1)}} {
		if _, err := agentRecoveryContextDigest(context); !errors.Is(err, ErrAgentRunMetadataInvalid) {
			t.Fatal("invalid or oversized context acquired a custody digest", err)
		}
	}
}

func TestRecoveryGenerationCannotOverflowEvenWithAnOtherwiseValidScope(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	run, _, err := control.AgentRunMetadata(input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	run.SchemaVersion, run.RuntimeRevision = 3, 2
	context := &agentv1.AgentRunRecoveryContext{SchemaVersion: 1, RunId: run.RunID, Generation: math.MaxInt64,
		MetadataEpoch: run.LastEvent.ExecutionEpoch, ExpectedIndex: 2, ExpectedRuntimeRevision: 1,
		ExpectedRequest: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("7", 64), Length: 20},
		PreviousScope: &agentv1.AgentRunRecoveryScope{Generation: math.MaxInt64 - 1, StartIndex: 1,
			ContextSha256: strings.Repeat("8", 64), Context: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("9", 64), Length: 20}}}
	digest, err := agentRecoveryContextDigest(context)
	if err != nil {
		t.Fatal(err)
	}
	run.Recovery = &agent.RunRecoveryScope{Generation: math.MaxInt64, Context: input.Context, StartIndex: 2,
		ContextSHA256: digest, Metadata: context}
	if !validAgentRecoveryScope(run) {
		t.Fatal("fixture is not a valid maximum generation")
	}
	expected := input.AgentRunRecoveryExpected
	expected.ExpectedRuntimeRevision = run.RuntimeVersion()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := control.agentRecoveryContextTx(tx, recoveryBoundaryRecord(t, run), expected); !errors.Is(err, ErrRevisionConflict) {
		t.Fatal("recovery generation overflow was not rejected", err)
	}
}

func TestRecoveryPublicCallsDoNotConvertAnUnavailableDatabaseToAnEmptyRun(t *testing.T) {
	control, input := recoveryAdmissionFixture(t, "cancelled")
	if err := control.db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.PreviewAgentRunRecovery(input.AgentRunRecoveryExpected); err == nil {
		t.Fatal("closed control store supplied a custody context")
	}
	if _, err := control.CommitAgentRunRecovery(input); err == nil {
		t.Fatal("closed control store accepted a recovery")
	}
}

func TestRecoveryReplayRejectsMissingMalformedAndUnreadableHeaders(t *testing.T) {
	for _, fault := range []string{"bounds", "empty", "oversized", "closed_transaction", "missing", "invalid_json", "null_payload"} {
		t.Run(fault, func(t *testing.T) {
			control, input := recoveryAdmissionFixture(t, "cancelled")
			result, err := control.CommitAgentRunRecovery(input)
			if err != nil {
				t.Fatal(err)
			}
			first, last, count := input.ExpectedIndex, result.Run.NextIndex, countControlEvents(t, control)
			view := false
			switch fault {
			case "bounds":
				first = 0
			case "empty":
				last = first
			case "oversized":
				last = first + 65
			case "missing":
				view = true
				recoveryBoundaryExec(t, control, fmt.Sprintf(`CREATE TEMP VIEW control_events AS SELECT * FROM main.control_events WHERE revision<>%d`, last))
			case "invalid_json", "null_payload":
				view = true
				value := "'{'"
				if fault == "null_payload" {
					value = "NULL"
				}
				recoveryBoundaryExec(t, control, fmt.Sprintf(`CREATE TEMP VIEW control_events AS SELECT domain,record_id,revision,
                    CASE WHEN domain='agent_run' AND revision=%d THEN %s ELSE payload_json END AS payload_json FROM main.control_events`, first+1, value))
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			if fault == "closed_transaction" {
				_ = tx.Rollback()
			}
			_, rejected := recoveryReplayEventsTx(tx, input.RunID, first, last)
			_ = tx.Rollback()
			if view {
				recoveryBoundaryExec(t, control, "DROP VIEW temp.control_events")
			}
			if rejected == nil || countControlEvents(t, control) != count {
				t.Fatal("unverified replay headers were accepted or changed history", rejected)
			}
		})
	}
}
