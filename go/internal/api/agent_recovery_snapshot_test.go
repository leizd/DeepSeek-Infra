package api

import (
	"bytes"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func TestAgentRunSnapshotExposesOnlyTheImmutableRecoveryHeader(t *testing.T) {
	const marker = "original-proof-body-is-not-a-public-snapshot"
	run := agent.RunMetadata{RunID: "recovery-snapshot", Status: "running", NextIndex: 10, RuntimeRevision: 2,
		Recovery: &agent.RunRecoveryScope{Generation: 1, StartIndex: 4, ContextSHA256: strings.Repeat("a", 64),
			Context: agent.ArtifactRef{SHA256: strings.Repeat("b", 64), Length: 100},
			Metadata: &agentv1.AgentRunRecoveryContext{SchemaVersion: 1,
				Plan: []*agentv1.AgentRunPlanNode{{Id: "coder", Task: marker}}}}}
	value := runProtocol(run)
	if value.RecoveryScope == nil || value.RecoveryScope.Generation != 1 || value.RecoveryScope.StartIndex != 4 ||
		value.RecoveryScope.ContextSha256 != run.Recovery.ContextSHA256 || value.RecoveryScope.Context.Sha256 != run.Recovery.Context.SHA256 ||
		value.RecoveryScope.Context.Length != run.Recovery.Context.Length {
		t.Fatal("snapshot lost the immutable recovery binding")
	}
	wire, err := proto.Marshal(value)
	if err != nil || bytes.Contains(wire, []byte(marker)) {
		t.Fatal("public snapshot exposed the retained proof body", err)
	}
	run.Recovery = nil
	if runProtocol(run).RecoveryScope != nil {
		t.Fatal("ordinary run acquired an invented recovery scope")
	}
}
