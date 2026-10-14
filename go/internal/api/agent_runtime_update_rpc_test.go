package api

import (
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestAgentRuntimeUpdateRPCPersistsOnlyCurrentImmutableReferences(t *testing.T) {
	control := agentRPCStore(t)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	authority, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	for index := int64(0); index < 4; index++ {
		input := agentRPCAppend(index, authority.MetadataEpoch)
		if index == 2 {
			input.Event.Type, input.Event.Status, input.Event.HasPlan = "agent_plan", "", true
			input.Event.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}}
		}
		if index == 3 {
			input.Event.Status = "awaiting_plan"
		}
		response, err := client.AppendEvent(ctx, input)
		if err != nil || response.Run.RuntimeRevision != 1 {
			t.Fatalf("initial runtime metadata: %+v %v", response, err)
		}
	}
	input := agentRPCAppend(4, authority.MetadataEpoch)
	input.Event.Status = "running"
	input.RuntimeUpdate = &agentv1.AgentRunRuntimeUpdate{ExpectedRuntimeRevision: 1,
		ExpectedRequest: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 100},
		Request:         &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 120}}
	input.RuntimeUpdate.ExpectedRuntimeRevision = 2
	if _, err := client.AppendEvent(ctx, input); status.Code(err) != codes.Aborted {
		t.Fatalf("stale runtime revision: %v", err)
	}
	input.RuntimeUpdate.ExpectedRuntimeRevision = 1
	previous := input.RuntimeUpdate.ExpectedRequest
	input.RuntimeUpdate.ExpectedRequest = nil
	if _, err := client.AppendEvent(ctx, input); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("missing previous request: %v", err)
	}
	input.RuntimeUpdate.ExpectedRequest = previous
	response, err := client.AppendEvent(ctx, input)
	if err != nil || response.Run.RuntimeRevision != 2 || response.Run.Request.Sha256 != strings.Repeat("c", 64) || response.Run.Status != "running" {
		t.Fatalf("runtime swap and confirmation: %+v %v", response, err)
	}
	replay, err := client.AppendEvent(ctx, input)
	if err != nil || !replay.AlreadyApplied || replay.Run.RuntimeRevision != 2 || replay.Run.NextIndex != 5 {
		t.Fatalf("runtime replay: %+v %v", replay, err)
	}
	read, err := client.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: input.RunId, Fence: input.Fence})
	if err != nil || read.Run.RuntimeRevision != 2 || read.Run.Request.Sha256 != strings.Repeat("c", 64) {
		t.Fatalf("runtime readback: %+v %v", read, err)
	}
}
