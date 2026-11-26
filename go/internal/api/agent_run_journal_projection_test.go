package api

import (
	"context"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// projectJournalObservation classifies a journal read and only appends when the
// read is unresolved. The dedupe helper behind it was uncovered, so a repeated
// unresolved read could have appended the same observation twice.

func appendNodeEffect(t *testing.T, driver *agentRunDriver, run agent.RunMetadata, phase, status, scope, sha string, length uint64) agent.RunMetadata {
	t.Helper()
	ctx := context.Background()
	authority, err := driver.service.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	mutation := &agentv1.AppendAgentRunEventRequest{
		SchemaVersion: 1,
		OperationId:   "node-effect-" + phase + "-" + status + "-" + scope,
		Actor:         "operator:agent-test",
		RunId:         run.RunID,
		ExpectedIndex: run.NextIndex,
		Fence:         &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch},
		Event: &agentv1.AgentRunEventMetadata{
			Type:   "node_effect",
			Phase:  phase,
			Status: status,
			Scope:  scope,
			Body:   &agentv1.AgentArtifactReference{Sha256: sha, Length: length},
		},
	}
	if _, err := driver.service.AppendEvent(ctx, mutation); err != nil {
		t.Fatalf("append node_effect: %v", err)
	}
	return driverFixtureRun(t, driver, run.RunID)
}

func TestJournalObservationIsAppendedOnlyOncePerUnresolvedRead(t *testing.T) {
	driver, _, run := newDriverFixture(t, false)
	control := driver.service.control
	ctx := context.Background()

	// An unresolved read projects one observation.
	before := driverFixtureRun(t, driver, run.RunID).NextIndex
	if err := driver.projectJournalObservation(run.RunID, "coder", nil, context.DeadlineExceeded, true); err != nil {
		t.Fatalf("first observation: %v", err)
	}
	after := driverFixtureRun(t, driver, run.RunID)
	if after.NextIndex != before+1 {
		t.Fatalf("observation did not append: %d -> %d", before, after.NextIndex)
	}

	// The identical read is recognised as already public and appends nothing.
	again := driverFixtureRun(t, driver, run.RunID).NextIndex
	if err := driver.projectJournalObservation(run.RunID, "coder", nil, context.DeadlineExceeded, true); err != nil {
		t.Fatalf("repeat observation: %v", err)
	}
	if got := driverFixtureRun(t, driver, run.RunID).NextIndex; got != again {
		t.Fatalf("duplicate observation appended: %d -> %d", again, got)
	}

	// A settled read never appends, whatever the dedupe would have decided.
	settled := driverFixtureRun(t, driver, run.RunID).NextIndex
	effect := &agentv1.AgentNodeEffectMetadata{State: "CONFIRMED"}
	if err := driver.projectJournalObservation(run.RunID, "coder", effect, nil, true); err != nil {
		t.Fatalf("settled observation: %v", err)
	}
	if got := driverFixtureRun(t, driver, run.RunID).NextIndex; got != settled {
		t.Fatal("a settled read advanced the cursor")
	}

	// With publish disabled the classification still runs but writes nothing.
	quiet := driverFixtureRun(t, driver, run.RunID).NextIndex
	if err := driver.projectJournalObservation(run.RunID, "critic", nil, context.DeadlineExceeded, false); err != nil {
		t.Fatalf("quiet observation: %v", err)
	}
	if got := driverFixtureRun(t, driver, run.RunID).NextIndex; got != quiet {
		t.Fatal("a non-publishing observation advanced the cursor")
	}
	_ = ctx
	_ = control
}

func TestJournalObservationDedupeMatchesThePublishedDocument(t *testing.T) {
	driver, _, run := newDriverFixture(t, false)
	control := driver.service.control

	// Publish one observation, then confirm the helper reports it as public.
	decision := agent.ProjectJournalObservation("coder", false, "", nil)
	if !decision.Append {
		t.Fatal("a missing read should be observable")
	}
	if err := driver.appendJournalObservation(run.RunID, decision); err != nil {
		t.Fatalf("append observation: %v", err)
	}
	same, err := journalObservationAlreadyPublic(control, run.RunID, decision)
	if err != nil {
		t.Fatal(err)
	}
	if !same {
		t.Fatal("the published document was not recognised")
	}

	// A different node has no matching document.
	other := agent.ProjectJournalObservation("critic", false, "", nil)
	same, err = journalObservationAlreadyPublic(control, run.RunID, other)
	if err != nil {
		t.Fatal(err)
	}
	if same {
		t.Fatal("an unpublished node was reported as public")
	}

	// A run that does not exist cannot be read, and the helper surfaces that as
	// an error rather than silently reporting "already public".
	if _, err := journalObservationAlreadyPublic(control, "run_without_history", decision); err == nil {
		t.Fatal("an absent run was read as public")
	}
}

var _ = store.CutoverGoAuthoritative
