package store

import (
	"errors"
	"testing"
)

func TestSynthesisSourceReadFaultsCannotAuthorizeAndLeaveOriginalProofRecoverable(t *testing.T) {
	for name, projection := range map[string]string{
		"null-source-id":       "NULL",
		"row-evaluation-error": "json_extract('broken', '$')",
	} {
		t.Run(name, func(t *testing.T) {
			control, input := tasksCompletionFixture(t, false)
			if _, err := control.CompleteAgentTasks(input); err != nil {
				t.Fatal(err)
			}
			before, err := control.ExportSnapshot()
			if err != nil {
				t.Fatal(err)
			}
			run, found, err := control.AgentRunMetadata(input.RunID)
			if err != nil || !found {
				t.Fatal("completed run missing", err)
			}
			scope := AgentRunExecutionBinding{SchemaVersion: 1, RunID: run.RunID, Phase: "synthesis", MetadataIndex: run.NextIndex,
				MetadataEpoch: synthesisRequest(t, control).Fence.ExecutionEpoch, Request: run.Request, PlanDigest: executionPlanDigest(run)}
			// A connection-local read fault leaves durable rows and immutable triggers intact.
			if _, err := control.db.Exec("CREATE TEMP VIEW action_journal AS SELECT " + projection + " AS id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,updated_at FROM main.action_journal"); err != nil {
				t.Fatal(err)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			if _, err := control.verifiedAgentSynthesisSourceTx(tx, scope); err == nil {
				t.Fatal("unreadable source authorized synthesis")
			}
			if err := tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec("DROP VIEW temp.action_journal"); err != nil {
				t.Fatal(err)
			}
			after, err := control.ExportSnapshot()
			if err != nil || after.Digest != before.Digest {
				t.Fatalf("read failure changed durable proof: %v", err)
			}
			if _, err := control.ClaimAgentRunExecution(synthesisRequest(t, control)); err != nil {
				t.Fatalf("read fault damaged later legitimate admission: %v", err)
			}
		})
	}
}

func TestAgentReplayProvenanceNeverInventsTimeOrCountWhenJournalIsUnavailable(t *testing.T) {
	control, input := tasksCompletionFixture(t, false)
	if _, err := control.CompleteAgentTasks(input); err != nil {
		t.Fatal(err)
	}
	before, err := control.ExportSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("CREATE TEMP VIEW " + operatorMutationTable + " AS SELECT operation_id,NULL AS recorded_at FROM main." + operatorMutationTable); err != nil {
		t.Fatal(err)
	}
	if when, found, err := control.OperatorMutationRecordedAt(input.OperationID); err == nil || found || !when.IsZero() {
		t.Fatalf("unreadable time became a successful replay timestamp: %v %v %v", when, found, err)
	}
	if _, err := control.db.Exec("DROP VIEW temp." + operatorMutationTable); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("CREATE TEMP VIEW " + operatorMutationTable + " AS SELECT operation_id FROM main." + operatorMutationTable + " WHERE json_extract('broken','$')=1"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.OperatorJournalSize(); err == nil {
		t.Fatal("unreadable journal became an empty journal")
	}
	if _, err := control.db.Exec("DROP VIEW temp." + operatorMutationTable); err != nil {
		t.Fatal(err)
	}
	when, found, err := control.OperatorMutationRecordedAt(input.OperationID)
	if err != nil || !found || when.IsZero() {
		t.Fatalf("original receipt lost after read recovery: %v", err)
	}
	after, err := control.ExportSnapshot()
	if err != nil || before.Digest != after.Digest {
		t.Fatalf("provenance read altered the journal: %v", err)
	}
	// Model an inactive connection and then a closed connection, preserving disk state.
	schema := control.schema
	control.schema = SchemaV15 - 1
	if _, err := control.OperatorJournalSize(); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("inactive schema reported a valid count: %v", err)
	}
	if _, found, err := control.OperatorMutationRecordedAt(input.OperationID); !errors.Is(err, ErrSchemaInactive) || found {
		t.Fatalf("inactive schema reported a valid timestamp: %v", err)
	}
	control.schema = schema
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.OperatorJournalSize(); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed writer reported a valid count: %v", err)
	}
	if _, found, err := control.OperatorMutationRecordedAt(input.OperationID); !errors.Is(err, ErrWriterFenceHeld) || found {
		t.Fatalf("closed writer reported a valid timestamp: %v", err)
	}
}
