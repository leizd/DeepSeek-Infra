package store

import (
	"bytes"
	"encoding/json"
)

// A private metadata append retains the action's public state. Recognize that
// exact append again when reading history, without allowing arbitrary self edges.
func nativeAgentMetadataHistoryEdge(before, after Record) bool {
	if before.Domain != "action" || after.Domain != before.Domain || before.ID != after.ID ||
		before.State != "EXECUTING" || after.State != before.State ||
		after.Revision != before.Revision+1 || after.ExecutionEpoch != before.ExecutionEpoch ||
		!nativeAgentExecution(before.Payload) || !sameAgentExecutionIntent(before, after) {
		return false
	}
	for _, field := range []string{agentNodeDispatchField, agentNodeOutcomeField} {
		var first, second map[string]json.RawMessage
		if json.Unmarshal(before.Payload, &first) != nil || json.Unmarshal(after.Payload, &second) != nil {
			return false
		}
		delete(first, field)
		delete(second, field)
		left, err := json.Marshal(first)
		right, otherErr := json.Marshal(second)
		if err != nil || otherErr != nil || !bytes.Equal(left, right) {
			continue
		}
		if field == agentNodeDispatchField {
			oldNodes, err := readAgentNodeDispatches(before)
			newNodes, otherErr := readAgentNodeDispatches(after)
			if err == nil && otherErr == nil && nativeMetadataMapAppend(oldNodes, newNodes) {
				return true
			}
		} else {
			oldOutcomes, err := agentNodeOutcomeBytes(before)
			newOutcomes, otherErr := agentNodeOutcomeBytes(after)
			nodes, nodeErr := readAgentNodeDispatches(after)
			if err != nil || otherErr != nil || nodeErr != nil || !nativeMetadataMapAppend(oldOutcomes, newOutcomes) {
				continue
			}
			valid := true
			for id, outcome := range newOutcomes {
				if _, present := nodes[id]; !present || outcome.WriterFencingToken <= 0 ||
					len(outcome.Receipt) == 0 || len(outcome.Receipt) > maximumPayloadBytes {
					valid = false
					break
				}
			}
			if valid {
				return true
			}
		}
	}
	return false
}

func nativeMetadataMapAppend[T any](before, after map[string]T) bool {
	if len(after) != len(before)+1 {
		return false
	}
	for id, previous := range before {
		current, present := after[id]
		if !present {
			return false
		}
		left, err := json.Marshal(previous)
		right, otherErr := json.Marshal(current)
		if err != nil || otherErr != nil || !bytes.Equal(left, right) {
			return false
		}
	}
	return true
}
