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

func TestNativeAgentHistoryPreservesEarlierNodeIntentsDuringAppend(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	before, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	nodes, err := readAgentNodeDispatches(before)
	if err != nil {
		t.Fatal(err)
	}
	original := nodes[input.NodeID]
	additional := original
	additional.NodeID, additional.Task = "critic", "review"
	additional.DependsOn = []string{"coder"}
	additional.OperationID = agentNodeOperationID(additional)
	for _, change := range []string{"append", "rewrite", "replace"} {
		t.Run(change, func(t *testing.T) {
			candidateNodes := map[string]AgentNodeDispatch{input.NodeID: original, additional.NodeID: additional}
			if change == "rewrite" {
				changed := original
				changed.Task = "another provider operation"
				changed.OperationID = agentNodeOperationID(changed)
				candidateNodes[input.NodeID] = changed
			} else if change == "replace" {
				delete(candidateNodes, input.NodeID)
				replacement := additional
				replacement.NodeID = "researcher"
				replacement.OperationID = agentNodeOperationID(replacement)
				candidateNodes[replacement.NodeID] = replacement
			}
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(before.Payload, &fields); err != nil {
				t.Fatal(err)
			}
			fields[agentNodeDispatchField], _ = json.Marshal(candidateNodes)
			candidate := before
			candidate.Revision++
			candidate.Payload, _ = json.Marshal(fields)
			if got := nativeAgentMetadataHistoryEdge(before, candidate); got != (change == "append") {
				t.Fatalf("history replaced an earlier reserved provider operation: %s", change)
			}
		})
	}
}
