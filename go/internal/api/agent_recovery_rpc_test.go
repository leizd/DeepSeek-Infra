package api

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func recoveryRPCClients(t *testing.T, control *store.Control, health ControlPlaneHealth) (agentv1.AgentRunControlClient, agentv1.AgentRunRecoveryControlClient) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := NewControlRPC(control, testInternalBearer, health)
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		server.Stop()
		<-done
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close(); server.Stop(); <-done })
	return agentv1.NewAgentRunControlClient(connection), agentv1.NewAgentRunRecoveryControlClient(connection)
}

func recoveryRPCFixture(t *testing.T) (*store.Control, agentv1.AgentRunControlClient, agentv1.AgentRunRecoveryControlClient, *agentv1.PreviewAgentRunRecoveryRequest) {
	t.Helper()
	control := agentExecutionRPCStore(t, true)
	runs, recovery := recoveryRPCClients(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	authority, err := runs.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	for index := int64(0); index < 6; index++ {
		input := agentRPCAppend(index, authority.MetadataEpoch)
		switch index {
		case 2:
			input.Event.Type, input.Event.Status, input.Event.HasPlan = "agent_plan", "", true
			input.Event.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}}
		case 3:
			input.Event.Status = "awaiting_plan"
		case 4:
			input.Event.Status = "running"
		case 5:
			input.Event.Status = "cancelled"
		}
		if _, err := runs.AppendEvent(ctx, input); err != nil {
			t.Fatal(err)
		}
	}
	return control, runs, recovery, &agentv1.PreviewAgentRunRecoveryRequest{SchemaVersion: 1, RunId: "run_rpc",
		MetadataFence: &commonv1.ActionFence{ActionId: "run_rpc", ExecutionEpoch: authority.MetadataEpoch}, ExpectedIndex: 6,
		ExpectedRuntimeRevision: 1, ExpectedRequest: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 100},
		Selection: &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME}}
}

func recoveryRPCCommit(input *agentv1.PreviewAgentRunRecoveryRequest, preview *agentv1.PreviewAgentRunRecoveryResponse) *agentv1.CommitAgentRunRecoveryRequest {
	result := &agentv1.CommitAgentRunRecoveryRequest{SchemaVersion: 1, OperationId: "rpc-recovery", Actor: "operator:rpc-recovery",
		RunId: input.RunId, Fence: proto.Clone(input.MetadataFence).(*commonv1.ActionFence), ExpectedIndex: input.ExpectedIndex, ExpectedRuntimeRevision: input.ExpectedRuntimeRevision,
		ExpectedRequest: proto.Clone(input.ExpectedRequest).(*agentv1.AgentArtifactReference), Selection: proto.Clone(input.Selection).(*agentv1.AgentRunRecoverySelection), ContextSha256: preview.ContextSha256,
		Context: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("e", 64), Length: 512},
		Request: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("d", 64), Length: 130}}
	for index := 0; index < 3; index++ {
		result.EventBodies = append(result.EventBodies, &agentv1.AgentArtifactReference{Sha256: strings.Repeat(fmt.Sprintf("%x", index+1), 64), Length: 40})
	}
	return result
}

func TestAgentRecoveryRPCUsesRealAuthorityTransactionAndStableReplay(t *testing.T) {
	control, runs, recovery, input := recoveryRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	before, _, err := control.AgentRunMetadata(input.RunId)
	if err != nil {
		t.Fatal(err)
	}
	preview, err := recovery.Preview(ctx, input)
	if err != nil || !preview.Authoritative || preview.Context == nil || preview.Context.Generation != 1 ||
		len(preview.Context.Execute) != 1 || preview.Context.Execute[0].Task != "write" {
		t.Fatal("private RPC did not return the authoritative detached recovery context", preview, err)
	}
	after, _, err := control.AgentRunMetadata(input.RunId)
	if err != nil || after.NextIndex != before.NextIndex || after.Recovery != nil {
		t.Fatal("read-only RPC preview wrote a recovery generation", err)
	}
	mutation := recoveryRPCCommit(input, preview)
	committed, err := recovery.Commit(ctx, mutation)
	if err != nil || committed.AlreadyApplied || committed.Run.RuntimeRevision != 2 || committed.Run.NextIndex != 9 ||
		committed.Run.Status != "running" || committed.Run.RecoveryScope == nil || committed.Run.RecoveryScope.ContextSha256 != preview.ContextSha256 ||
		len(committed.Events) != 3 || committed.Events[0].Type != "run_status" || committed.Events[1].Type != "agent_reset" || committed.Events[2].Type != "final_reset" {
		t.Fatal("RPC recovery was partial or bypassed the real store", committed, err)
	}
	raw, _, err := control.AgentRunMetadata(input.RunId)
	if err != nil || !raw.Cancelled || raw.CancellationActive() || raw.Recovery == nil || raw.Recovery.Generation != 1 {
		t.Fatal("RPC erased observed cancellation or failed to bind its generation", err)
	}
	replay, err := recovery.Commit(ctx, mutation)
	if err != nil || !replay.AlreadyApplied || !proto.Equal(replay.Run, committed.Run) || len(replay.Events) != len(committed.Events) {
		t.Fatal("RPC lost its original idempotent receipt", replay, err)
	}
	for index := range replay.Events {
		if !proto.Equal(replay.Events[index], committed.Events[index]) {
			t.Fatal("RPC replay rebound an original event")
		}
	}
	read, err := runs.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: input.RunId, Fence: input.MetadataFence})
	if err != nil || !proto.Equal(read.Run, committed.Run) {
		t.Fatal("ordinary read did not retain the immutable recovery header", err)
	}
	if actions, err := control.ListAuthoritativeRecords("action"); err != nil || len(actions) != 0 {
		t.Fatal("metadata RPC granted a worker effect", err)
	}
}

func TestAgentRecoveryRPCRequiresBearerAuthorityAndAnAvailableStore(t *testing.T) {
	control, _, recovery, input := recoveryRPCFixture(t)
	if _, err := recovery.Preview(context.Background(), input); status.Code(err) != codes.Unauthenticated {
		t.Fatal("unauthenticated recovery read", err)
	}
	if _, err := recovery.Commit(context.Background(), &agentv1.CommitAgentRunRecoveryRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatal("unauthenticated recovery write", err)
	}
	for _, row := range []struct {
		control *store.Control
		health  ControlPlaneHealth
		code    codes.Code
	}{{control, ShadowHealth(), codes.PermissionDenied}, {nil, HealthFor(config.Config{Mode: config.ModeAuthoritative}), codes.Unavailable}} {
		_, client := recoveryRPCClients(t, row.control, row.health)
		ctx := controlRPCContext(t, testInternalBearer)
		if _, err := client.Preview(ctx, input); status.Code(err) != row.code {
			t.Fatal("non-authoritative RPC prepared a recovery", err)
		}
		if _, err := client.Commit(ctx, &agentv1.CommitAgentRunRecoveryRequest{}); status.Code(err) != row.code {
			t.Fatal("non-authoritative RPC committed a recovery", err)
		}
	}
}

func TestAgentRecoveryRPCRejectsUnsupportedNestedProtocolFields(t *testing.T) {
	control, _, recovery, input := recoveryRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	preview, err := recovery.Preview(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"root", "fence", "request", "selection"} {
		bad := proto.Clone(input).(*agentv1.PreviewAgentRunRecoveryRequest)
		var selected proto.Message = bad
		switch field {
		case "fence":
			selected = bad.MetadataFence
		case "request":
			selected = bad.ExpectedRequest
		case "selection":
			selected = bad.Selection
		}
		selected.ProtoReflect().SetUnknown([]byte{0x80, 0x06, 0x01})
		if _, err := recovery.Preview(ctx, bad); status.Code(err) != codes.InvalidArgument {
			t.Fatal("unsupported preview fields were erased before admission", field, err)
		}
	}
	for _, field := range []string{"root", "fence", "expected_request", "selection", "context", "request", "event"} {
		bad := recoveryRPCCommit(input, preview)
		var selected proto.Message = bad
		switch field {
		case "fence":
			selected = bad.Fence
		case "expected_request":
			selected = bad.ExpectedRequest
		case "selection":
			selected = bad.Selection
		case "context":
			selected = bad.Context
		case "request":
			selected = bad.Request
		case "event":
			selected = bad.EventBodies[0]
		}
		selected.ProtoReflect().SetUnknown([]byte{0x80, 0x06, 0x01})
		if _, err := recovery.Commit(ctx, bad); status.Code(err) != codes.InvalidArgument {
			t.Fatal("unsupported commit fields were erased before admission", field, err)
		}
	}
	current, _, err := control.AgentRunMetadata(input.RunId)
	if err != nil || current.NextIndex != input.ExpectedIndex || current.Recovery != nil {
		t.Fatal("unsupported wire fields changed the current runtime", err)
	}
}

func TestAgentRecoveryRPCRejectsStaleOrMalformedWireRequests(t *testing.T) {
	control, _, recovery, input := recoveryRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	preview, err := recovery.Preview(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	for _, fault := range []string{"schema", "missing_reference", "fence", "epoch", "cursor", "role", "absent"} {
		bad := proto.Clone(input).(*agentv1.PreviewAgentRunRecoveryRequest)
		code := codes.InvalidArgument
		switch fault {
		case "schema":
			bad.SchemaVersion = 0
		case "missing_reference":
			bad.ExpectedRequest = nil
		case "fence":
			bad.MetadataFence.ActionId, code = "another-run", codes.FailedPrecondition
		case "epoch":
			bad.MetadataFence.ExecutionEpoch, code = bad.MetadataFence.ExecutionEpoch+1, codes.FailedPrecondition
		case "cursor":
			bad.ExpectedIndex, code = bad.ExpectedIndex+1, codes.Aborted
		case "role":
			bad.Selection.AgentId = "coder"
		case "absent":
			bad.RunId, bad.MetadataFence.ActionId, code = "absent-run", "absent-run", codes.NotFound
		}
		if _, err := recovery.Preview(ctx, bad); status.Code(err) != code {
			t.Fatal("invalid preview did not preserve its public error class", fault, err)
		}
	}
	for _, fault := range []string{"schema", "context", "event", "fence", "epoch", "cursor"} {
		bad := recoveryRPCCommit(input, preview)
		code := codes.InvalidArgument
		switch fault {
		case "schema":
			bad.SchemaVersion = 0
		case "context":
			bad.ContextSha256, code = strings.Repeat("7", 64), codes.Aborted
		case "event":
			bad.EventBodies[0] = nil
		case "fence":
			bad.Fence = &commonv1.ActionFence{ActionId: "another-run", ExecutionEpoch: input.MetadataFence.ExecutionEpoch}
			code = codes.FailedPrecondition
		case "epoch":
			bad.Fence = &commonv1.ActionFence{ActionId: input.RunId, ExecutionEpoch: input.MetadataFence.ExecutionEpoch + 1}
			code = codes.FailedPrecondition
		case "cursor":
			bad.ExpectedIndex, code = bad.ExpectedIndex+1, codes.Aborted
		}
		if _, err := recovery.Commit(ctx, bad); status.Code(err) != code {
			t.Fatal("invalid commit did not preserve its public error class", fault, err)
		}
	}
	current, _, err := control.AgentRunMetadata(input.RunId)
	if err != nil || current.NextIndex != input.ExpectedIndex || current.Recovery != nil {
		t.Fatal("rejected RPC changed the recovery cursor", err)
	}
}

func TestAgentRecoveryRPCErrorClassesPreserveFailClosedStorageMeaning(t *testing.T) {
	for _, row := range []struct {
		err  error
		code codes.Code
	}{{agent.ErrInvalidRunRecovery, codes.InvalidArgument}, {store.ErrAgentRunRecoveryUnsettled, codes.FailedPrecondition},
		{store.ErrAgentRunRecoveryRequired, codes.FailedPrecondition}, {store.ErrEmptyRecordID, codes.NotFound},
		{store.ErrCorruptRecord, codes.DataLoss}, {store.ErrRevisionConflict, codes.Aborted}, {errors.New("private SQL path"), codes.Unavailable}} {
		converted := agentRecoveryRPCError(row.err)
		if status.Code(converted) != row.code || strings.Contains(converted.Error(), "private SQL path") {
			t.Fatal("recovery error lost its meaning or exposed internal storage details", converted)
		}
	}
}
