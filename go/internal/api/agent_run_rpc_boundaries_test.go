package api

import (
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
)

// The public run snapshot must project a retained recovery scope and keep the
// legacy snapshot unchanged when no scope exists.

func TestRunProtocolProjectsTheRetainedRecoveryScope(t *testing.T) {
	plain := runProtocol(agent.RunMetadata{RunID: "run_protocol_plain"})
	if plain.RecoveryScope != nil {
		t.Fatal("a run without a recovery scope projected one")
	}
	retained := runProtocol(agent.RunMetadata{RunID: "run_protocol_scope",
		Recovery: &agent.RunRecoveryScope{Generation: 3, Context: agent.ArtifactRef{SHA256: "scope-context", Length: 12},
			ContextSHA256: "scope-digest", StartIndex: 4}})
	if retained.RecoveryScope == nil || retained.RecoveryScope.Generation != 3 || retained.RecoveryScope.StartIndex != 4 ||
		retained.RecoveryScope.Context.Sha256 != "scope-context" || retained.RecoveryScope.ContextSha256 != "scope-digest" {
		t.Fatalf("retained recovery scope was not projected: %v", retained.RecoveryScope)
	}
}
