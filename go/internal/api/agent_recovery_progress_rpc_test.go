package api

import (
	"context"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func progressRPCFixture(t *testing.T) (*agentRunDriver, *driverProgressFixture, agentv1.AgentRunControlClient, agentv1.AgentRunRecoveryControlClient, *agentv1.PreviewAgentRecoveryProgressRequest) {
	t.Helper()
	driver, fixture, run := driverProgressFixtureRun(t, "critic", false)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	runs, recovery := recoveryRPCClients(t, driver.service.control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	authority, err := runs.GetAuthority(controlRPCContext(t, testInternalBearer), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	return driver, fixture, runs, recovery, &agentv1.PreviewAgentRecoveryProgressRequest{SchemaVersion: 1, RunId: run.RunID,
		MetadataFence: &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch}, ExpectedIndex: run.NextIndex, RuntimeRevision: run.RuntimeVersion(), Request: artifactProtocol(run.Request)}
}

func progressRPCCommit(t *testing.T, fixture *driverProgressFixture, input *agentv1.PreviewAgentRecoveryProgressRequest, preview *agentv1.PreviewAgentRecoveryProgressResponse) *agentv1.CommitAgentRecoveryProgressRequest {
	t.Helper()
	proof, err := fixture.PrepareAgentRecoveryProgress(context.Background(), &agentv1.PrepareAgentRecoveryProgressRequest{SchemaVersion: 1, Context: preview.Context, Fence: input.MetadataFence})
	if err != nil {
		t.Fatal(err)
	}
	return &agentv1.CommitAgentRecoveryProgressRequest{SchemaVersion: 1, OperationId: "progress-private-rpc", Actor: agentDriverActor, RunId: input.RunId,
		MetadataFence: proto.Clone(input.MetadataFence).(*commonv1.ActionFence), ExpectedIndex: input.ExpectedIndex, RuntimeRevision: input.RuntimeRevision,
		Request: proto.Clone(input.Request).(*agentv1.AgentArtifactReference), Receipt: proof}
}

func TestProgressRPCUsesTheRealMetadataTransactionAndReplaysOriginalEvents(t *testing.T) {
	driver, fixture, runs, recovery, input := progressRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	preview, err := recovery.PreviewProgress(ctx, input)
	if err != nil || !preview.Authoritative || preview.Context == nil {
		t.Fatal(err)
	}
	before := driverFixtureRun(t, driver, input.RunId)
	if before.NextIndex != input.ExpectedIndex || before.RecoveryProgress != nil {
		t.Fatal("preview wrote progress metadata")
	}
	request := progressRPCCommit(t, fixture, input, preview)
	result, err := recovery.CommitProgress(ctx, request)
	if err != nil || result.AlreadyApplied || result.Run.Status != "done" || len(result.Events) != 2 || result.Events[0].Type != "agent_note" || result.Events[1].Status != "done" {
		t.Fatal("private progress RPC did not atomically finish the selected task", err)
	}
	append := agentRPCAppend(result.Run.NextIndex, input.MetadataFence.ExecutionEpoch)
	append.Create = nil
	append.OperationId = "after-progress-rpc"
	append.Event = &agentv1.AgentRunEventMetadata{Type: "agent_note", Phase: "critic", Body: driverFixtureReference("later-public-note")}
	advanced, err := runs.AppendEvent(ctx, append)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := recovery.CommitProgress(ctx, request)
	if err != nil || !replay.AlreadyApplied || !proto.Equal(replay.Run, advanced.Run) || len(replay.Events) != 2 || !proto.Equal(replay.Events[0], result.Events[0]) || !proto.Equal(replay.Events[1], result.Events[1]) {
		t.Fatal("RPC replay replaced the current run or original event identities", err)
	}
}

func TestProgressRPCRequiresControllerAuthorityAndRejectsNestedUnsupportedProofs(t *testing.T) {
	driver, fixture, _, recovery, input := progressRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	if _, err := recovery.PreviewProgress(context.Background(), input); status.Code(err) != codes.Unauthenticated {
		t.Fatal("unauthenticated progress read accepted", err)
	}
	preview, err := recovery.PreviewProgress(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	request := progressRPCCommit(t, fixture, input, preview)
	for _, field := range []string{"root", "fence", "request"} {
		bad := proto.Clone(input).(*agentv1.PreviewAgentRecoveryProgressRequest)
		var value proto.Message = bad
		switch field {
		case "fence":
			value = bad.MetadataFence
		case "request":
			value = bad.Request
		}
		value.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
		if _, err := recovery.PreviewProgress(ctx, bad); status.Code(err) != codes.InvalidArgument {
			t.Fatal("unsupported preview fields were erased", field, err)
		}
	}
	for _, field := range []string{"root", "fence", "request", "receipt", "context", "source", "evidence"} {
		bad := proto.Clone(request).(*agentv1.CommitAgentRecoveryProgressRequest)
		var value proto.Message = bad
		switch field {
		case "fence":
			value = bad.MetadataFence
		case "request":
			value = bad.Request
		case "receipt":
			value = bad.Receipt
		case "context":
			value = bad.Receipt.Evidence.Context
		case "source":
			value = bad.Receipt.Evidence.Context.Outputs[0].Dispatch
		case "evidence":
			value = bad.Receipt.Evidence.Context.Outputs[0].Outcome.Evidence
		}
		value.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
		if _, err := recovery.CommitProgress(ctx, bad); status.Code(err) != codes.InvalidArgument {
			t.Fatal("unsupported progress proof field was erased", field, err)
		}
	}
	for _, fault := range []string{"cursor", "fence", "epoch", "signature", "missing-proof"} {
		bad := proto.Clone(request).(*agentv1.CommitAgentRecoveryProgressRequest)
		code := codes.FailedPrecondition
		switch fault {
		case "cursor":
			bad.ExpectedIndex++
			code = codes.Aborted
		case "fence":
			bad.MetadataFence.ActionId = "foreign"
		case "epoch":
			bad.MetadataFence.ExecutionEpoch++
		case "signature":
			bad.Receipt.Signature[0] ^= 1
		case "missing-proof":
			bad.Receipt = nil
			code = codes.InvalidArgument
		}
		if _, err := recovery.CommitProgress(ctx, bad); status.Code(err) != code {
			t.Fatal("progress refusal changed its error class", fault, err)
		}
	}
	_, shadow := recoveryRPCClients(t, driver.service.control, HealthFor(config.Config{Mode: config.ModeShadow}))
	if _, err := shadow.PreviewProgress(ctx, input); status.Code(err) != codes.PermissionDenied {
		t.Fatal("shadow process granted a progress preview", err)
	}
	if _, err := shadow.CommitProgress(ctx, request); status.Code(err) != codes.PermissionDenied {
		t.Fatal("shadow process wrote progress", err)
	}
	after := driverFixtureRun(t, driver, input.RunId)
	if after.NextIndex != input.ExpectedIndex || after.RecoveryProgress != nil || len(fixture.calls) != 5 {
		t.Fatal("refused private RPC changed state or dispatched another effect")
	}
}
