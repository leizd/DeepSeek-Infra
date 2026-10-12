package store

import (
	"encoding/json"
	"testing"
)

func TestNativeAgentHistoryAcceptsOnlyOneImmutableMetadataAppend(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	before, found, err := control.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	if _, err := control.RecordAgentNodeOutcome(input); err != nil {
		t.Fatal(err)
	}
	after, found, err := control.Get("action", input.ActionID)
	if err != nil || !found || !nativeAgentMetadataHistoryEdge(before, after) {
		t.Fatalf("the committed metadata append is unreadable: %v", err)
	}
	for _, change := range []string{"noop", "epoch", "state", "revision", "unrelated", "delete", "rewrite", "missing_intent", "empty_receipt", "writer"} {
		t.Run(change, func(t *testing.T) {
			previous, candidate := before, after
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(candidate.Payload, &fields); err != nil {
				t.Fatal(err)
			}
			switch change {
			case "noop":
				candidate.Payload = previous.Payload
			case "epoch":
				candidate.ExecutionEpoch++
			case "state":
				candidate.State = "SUCCEEDED"
			case "revision":
				candidate.Revision++
			case "unrelated":
				fields["unrelated"] = json.RawMessage(`true`)
			case "delete":
				previous, candidate = after, before
				candidate.Revision = previous.Revision + 1
			case "rewrite":
				previous = after
				candidate.Revision++
				values, _ := agentNodeOutcomeBytes(candidate)
				value := values[input.NodeID]
				value.WriterFencingToken++
				values[input.NodeID] = value
				fields[agentNodeOutcomeField], _ = json.Marshal(values)
			case "missing_intent":
				fields[agentNodeDispatchField] = json.RawMessage(`{}`)
			case "empty_receipt", "writer":
				values, _ := agentNodeOutcomeBytes(candidate)
				value := values[input.NodeID]
				if change == "empty_receipt" {
					value.Receipt = nil
				} else {
					value.WriterFencingToken = 0
				}
				values[input.NodeID] = value
				fields[agentNodeOutcomeField], _ = json.Marshal(values)
			}
			if change == "unrelated" || change == "rewrite" || change == "missing_intent" || change == "empty_receipt" || change == "writer" {
				candidate.Payload, _ = json.Marshal(fields)
			}
			if nativeAgentMetadataHistoryEdge(previous, candidate) {
				t.Fatal("invalid history gained a native metadata exception")
			}
		})
	}
}
