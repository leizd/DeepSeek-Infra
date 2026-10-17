package store

import "testing"

func TestNativeAgentMetadataHistoryEdgeRefusesForeignPairs(t *testing.T) {
	if nativeAgentMetadataHistoryEdge(Record{Domain: "policy", ID: "p"}, Record{Domain: "policy", ID: "p"}) {
		t.Fatal("a non-action pair was accepted as a native metadata edge")
	}
	if nativeAgentMetadataHistoryEdge(Record{Domain: "action", ID: "a"}, Record{Domain: "agent_run", ID: "a"}) {
		t.Fatal("a cross-domain pair was accepted as a native metadata edge")
	}
}
