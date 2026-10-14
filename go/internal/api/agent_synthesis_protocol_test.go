package api

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestSynthesisProtocolRetainsCompleteAdmittedScopeAndCopiesDependencies(t *testing.T) {
	control := agentExecutionRPCStore(t, true)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	input := executionRPCInput(seedAgentExecutionRPC(t, client))
	result, err := control.ClaimAgentRunExecution(store.AgentRunExecutionRequest{OperationID: input.OperationId, Actor: input.Actor,
		RunID: input.RunId, Phase: "tasks", ExpectedIndex: input.ExpectedIndex, Fence: input.MetadataFence, Owner: input.Owner, LeaseSeconds: int64(input.LeaseSeconds)})
	if err != nil {
		t.Fatal(err)
	}
	// Convert a copied scope only to exercise the wire boundary. This does not
	// insert a synthesis admission, authorize an effect or fabricate run status.
	copied := result.Admission
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(copied.Record.Payload, &payload); err != nil {
		t.Fatal(err)
	}
	var binding store.AgentRunExecutionBinding
	if err := json.Unmarshal(payload["nativeAgentExecution"], &binding); err != nil {
		t.Fatal(err)
	}
	binding.Phase = "synthesis"
	sum := sha256.Sum256([]byte("agent-run-execution-v1\n" + binding.RunID + "\nsynthesis\n4\n"))
	copied.Record.ID = "agent-exec-" + hex.EncodeToString(sum[:])
	copied.Lease.ActionID = copied.Record.ID
	payload["nativeAgentExecution"], err = json.Marshal(binding)
	if err != nil {
		t.Fatal(err)
	}
	copied.Record.Payload, err = json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := executionClaimProtocol(copied, result.WriterLeaseUntil)
	if err != nil || claim.Phase != agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS ||
		claim.Fence.ActionId != copied.Record.ID || claim.Request.Sha256 != binding.Request.SHA256 ||
		claim.PlanDigest != binding.PlanDigest || claim.MetadataIndex != binding.MetadataIndex || claim.MetadataEpoch != binding.MetadataEpoch ||
		claim.Owner != copied.Lease.Owner || claim.ClaimToken != copied.Lease.ClaimToken || claim.WriterFencingToken != copied.Lease.WriterFencingToken {
		t.Fatalf("wire projection rebound the admitted synthesis scope: %+v %v", claim, err)
	}
	if executionPhaseName(claim.Phase) != "synthesis" {
		t.Fatal("synthesis request is reinterpreted as a task")
	}
	node := store.AgentNodeDispatch{SchemaVersion: 1, ActionID: claim.Fence.ActionId, ExecutionEpoch: claim.Fence.ExecutionEpoch, RunID: claim.RunId,
		NodeID: "synthesizer", Phase: "synthesis", Task: "synthesize verified agent outputs", DependsOn: []string{"coder", "critic"}, Request: binding.Request,
		PlanDigest: binding.PlanDigest, MetadataIndex: binding.MetadataIndex, MetadataEpoch: binding.MetadataEpoch}
	wire := nodeDispatchProtocol(node)
	if wire.Phase != claim.Phase || wire.NodeId != "synthesizer" || len(wire.DependsOn) != 2 || wire.Request.Sha256 != claim.Request.Sha256 {
		t.Fatal("node scope lost across the RPC boundary")
	}
	wire.DependsOn[0] = "changed"
	if node.DependsOn[0] != "coder" {
		t.Fatal("RPC consumer changed Go's selected predecessor list")
	}
}
