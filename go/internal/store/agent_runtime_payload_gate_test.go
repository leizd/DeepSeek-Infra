package store

import (
	"errors"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

// The runtime_payload gate reads its fields out of the event's status and scope
// columns, so a malformed or non-advancing revision must be refused before the
// metadata is rewritten. These cases pin that boundary; the whole branch was
// uncovered.

func runtimePayloadEvent(phase, status, scope string) agent.RunEvent {
	return agent.RunEvent{Type: "runtime_payload", Phase: phase, Status: status, Scope: scope}
}

func createRun(t *testing.T, control *Control) int64 {
	t.Helper()
	create := eventMutation(t, control, 0, agent.RunEvent{Type: "run_status", Status: "created"})
	result, err := control.AppendAgentRunEvent(create)
	if err != nil {
		t.Fatal(err)
	}
	return result.Run.NextIndex
}

func TestRuntimePayloadRefusesAMalformedOrNonAdvancingRevision(t *testing.T) {
	digest := strings.Repeat("a", 64)
	cases := []struct {
		name  string
		event agent.RunEvent
	}{
		{"non-numeric-length", runtimePayloadEvent(digest, "not-a-number", "2")},
		{"non-numeric-revision", runtimePayloadEvent(digest, "30", "not-a-number")},
		{"bad-phase", runtimePayloadEvent("not-a-digest", "30", "2")},
		{"zero-length", runtimePayloadEvent(digest, "0", "2")},
		{"oversized-length", runtimePayloadEvent(digest, "2000000", "2")},
		{"revision-does-not-advance", runtimePayloadEvent(digest, "30", "1")},
		{"revision-gap", runtimePayloadEvent(digest, "30", "4")},
	}
	for _, row := range cases {
		t.Run(row.name, func(t *testing.T) {
			control := freshAgentEventControl(t)
			t.Cleanup(func() { _ = control.Close() })
			index := createRun(t, control)
			mutation := eventMutation(t, control, index, row.event)
			mutation.Create = nil
			if _, err := control.AppendAgentRunEvent(mutation); !errors.Is(err, ErrAgentRunMetadataInvalid) {
				t.Fatalf("%s was accepted: %v", row.name, err)
			}
		})
	}
}

func TestRuntimePayloadAcceptsExactlyTheNextRevision(t *testing.T) {
	control := freshAgentEventControl(t)
	t.Cleanup(func() { _ = control.Close() })
	digest := strings.Repeat("a", 64)
	index := createRun(t, control)

	// A payload append validates the revision but does not itself move it: the
	// metadata revision advances only through the runtime-update transaction.
	// Here the accepted contract is that revision 2 is legal and revision 3 is
	// not, because the current admitted revision is still 1.
	mutation := eventMutation(t, control, index, runtimePayloadEvent(digest, "30", "2"))
	mutation.Create = nil
	result, err := control.AppendAgentRunEvent(mutation)
	if err != nil {
		t.Fatalf("the next revision was refused: %v", err)
	}
	if result.Run.NextIndex != index+1 {
		t.Fatalf("cursor did not advance: %d", result.Run.NextIndex)
	}
	if result.Run.AdmittedRevision() != 1 {
		t.Fatalf("a payload append changed the admitted revision to %d", result.Run.AdmittedRevision())
	}

	// Because the admitted revision is unchanged, the same revision is still the
	// legal successor and a later one is not.
	repeat := eventMutation(t, control, result.Run.NextIndex, runtimePayloadEvent(digest, "30", "2"))
	repeat.Create = nil
	repeat.OperationID = "runtime-payload-repeat"
	if _, err := control.AppendAgentRunEvent(repeat); err != nil {
		t.Fatalf("the same legal successor was refused: %v", err)
	}

	gap := eventMutation(t, control, result.Run.NextIndex+1, runtimePayloadEvent(digest, "30", "4"))
	gap.Create = nil
	gap.OperationID = "runtime-payload-gap"
	if _, err := control.AppendAgentRunEvent(gap); !errors.Is(err, ErrAgentRunMetadataInvalid) {
		t.Fatalf("a gapped revision was accepted: %v", err)
	}
}
