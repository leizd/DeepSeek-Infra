package api

import (
	"context"
	"net"
	"testing"

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

func agentNodeRPCClients(t *testing.T, control *store.Control, health ControlPlaneHealth) (agentv1.AgentRunControlClient, agentv1.AgentNodeControlClient) {
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
	return agentv1.NewAgentRunControlClient(connection), agentv1.NewAgentNodeControlClient(connection)
}

func agentNodeRPCFixture(t *testing.T) (*store.Control, agentv1.AgentRunControlClient, agentv1.AgentNodeControlClient, *agentv1.BeginAgentNodeDispatchRequest) {
	t.Helper()
	control := agentExecutionRPCStore(t, true)
	runs, nodes := agentNodeRPCClients(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	claim, err := runs.ClaimExecution(controlRPCContext(t, testInternalBearer), executionRPCInput(seedAgentExecutionRPC(t, runs)))
	if err != nil {
		t.Fatal(err)
	}
	return control, runs, nodes, &agentv1.BeginAgentNodeDispatchRequest{SchemaVersion: 1,
		OperationId: "rpc-node-coder", Actor: "operator:agent-test", Fence: claim.Claim.Fence,
		RunId: claim.Claim.RunId, NodeId: "coder", Owner: claim.Claim.Owner, ClaimToken: claim.Claim.ClaimToken}
}

func TestAgentNodeRPCUsesRegisteredAuthenticatedListenerAndImmutableTask(t *testing.T) {
	control, runs, nodes, input := agentNodeRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	query := &agentv1.GetAgentNodeDispatchRequest{Fence: input.Fence, RunId: input.RunId, NodeId: input.NodeId}
	absent, err := nodes.GetDispatch(ctx, query)
	if err != nil || absent.Found || absent.Intent != nil {
		t.Fatalf("read invented a dispatch or result: %+v %v", absent, err)
	}
	for _, bearer := range []string{"", "wrong"} {
		if _, err := nodes.BeginDispatch(controlRPCContext(t, bearer), input); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("unauthenticated node dispatch: %v", err)
		}
		if _, err := nodes.GetDispatch(controlRPCContext(t, bearer), query); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("unauthenticated node query: %v", err)
		}
	}
	first, err := nodes.BeginDispatch(ctx, input)
	if err != nil || !first.GetFresh() || first.GetIntent().GetTask() != "write" ||
		first.GetIntent().GetNodeId() != "coder" || first.GetClaim().GetState() != "EXECUTING" ||
		!proto.Equal(first.GetIntent().GetFence(), input.Fence) || first.GetIntent().GetState() != "INTENT_RECORDED" ||
		!proto.Equal(first.GetIntent().GetRequest(), first.GetClaim().GetRequest()) {
		t.Fatalf("control-selected intent missing: %+v %v", first, err)
	}
	for _, operation := range []string{input.OperationId, "rpc-node-another-id"} {
		retry := proto.Clone(input).(*agentv1.BeginAgentNodeDispatchRequest)
		retry.OperationId = operation
		replay, err := nodes.BeginDispatch(ctx, retry)
		if err != nil || replay.GetFresh() || !proto.Equal(replay.GetIntent(), first.GetIntent()) {
			t.Fatalf("lost response/new operation authorized redispatch: %+v %v", replay, err)
		}
	}
	read, err := nodes.GetDispatch(ctx, query)
	if err != nil || !read.Found || !proto.Equal(read.Intent, first.Intent) {
		t.Fatalf("durable query mismatch: %+v %v", read, err)
	}
	renewal, err := runs.RenewExecution(ctx, &agentv1.RenewAgentRunExecutionRequest{Fence: input.Fence,
		RunId: input.RunId, Owner: input.Owner, ClaimToken: input.ClaimToken})
	if err != nil || !proto.Equal(renewal.GetClaim(), first.GetClaim()) {
		t.Fatalf("dispatch changed immutable phase lease identity: %+v %v", renewal, err)
	}
	run, found, err := control.AgentRunMetadata(input.RunId)
	if err != nil || !found || run.NextIndex != 4 || run.Nodes()["coder"].Attempts != 0 {
		t.Fatalf("dispatch fabricated provider effects or metadata progress: %+v %v", run, err)
	}
}

func TestAgentNodeRPCRejectsScopeUnknownFieldsAndCancelledCalls(t *testing.T) {
	control, _, nodes, input := agentNodeRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	for _, test := range []struct {
		name string
		edit func(*agentv1.BeginAgentNodeDispatchRequest)
		want codes.Code
	}{
		{"schema", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.SchemaVersion = 2 }, codes.InvalidArgument},
		{"no-fence", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.Fence = nil }, codes.InvalidArgument},
		{"zero-epoch", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.Fence.ExecutionEpoch = 0 }, codes.InvalidArgument},
		{"future-epoch", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.Fence.ExecutionEpoch++ }, codes.FailedPrecondition},
		{"owner", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.Owner = "other" }, codes.FailedPrecondition},
		{"token", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.ClaimToken = "other" }, codes.FailedPrecondition},
		{"run", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.RunId = "another_run" }, codes.InvalidArgument},
		{"node", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.NodeId = "critic" }, codes.InvalidArgument},
		{"unknown", func(v *agentv1.BeginAgentNodeDispatchRequest) { v.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 0x01}) }, codes.InvalidArgument},
		{"fence-unknown", func(v *agentv1.BeginAgentNodeDispatchRequest) {
			v.Fence.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 0x01})
		}, codes.InvalidArgument},
	} {
		t.Run(test.name, func(t *testing.T) {
			bad := proto.Clone(input).(*agentv1.BeginAgentNodeDispatchRequest)
			test.edit(bad)
			if _, err := nodes.BeginDispatch(ctx, bad); status.Code(err) != test.want {
				t.Fatalf("invalid scope accepted or wrong error: %v", err)
			}
		})
	}
	service := &agentNodeService{run: &agentRunService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := service.BeginDispatch(cancelled, input); status.Code(err) != codes.Canceled {
		t.Fatalf("cancelled call changed control state: %v", err)
	}
	query := &agentv1.GetAgentNodeDispatchRequest{Fence: input.Fence, RunId: input.RunId, NodeId: input.NodeId}
	if _, err := service.GetDispatch(cancelled, query); status.Code(err) != codes.Canceled {
		t.Fatalf("cancelled query: %v", err)
	}
	if _, found, err := control.AgentNodeDispatch(input.Fence.ActionId, input.NodeId); err != nil || found {
		t.Fatalf("denied calls created a control intent: %v %v", found, err)
	}
	if _, err := service.BeginDispatch(context.Background(), nil); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("nil begin: %v", err)
	}
	if _, err := service.GetDispatch(context.Background(), nil); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("nil query: %v", err)
	}
}

func TestAgentNodeRPCQueryCannotAuthorizeOrReadAnotherFence(t *testing.T) {
	_, _, nodes, input := agentNodeRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	query := &agentv1.GetAgentNodeDispatchRequest{Fence: input.Fence, RunId: input.RunId, NodeId: input.NodeId}
	for _, test := range []struct {
		name string
		edit func(*agentv1.GetAgentNodeDispatchRequest)
		want codes.Code
	}{
		{"no-fence", func(v *agentv1.GetAgentNodeDispatchRequest) { v.Fence = nil }, codes.InvalidArgument},
		{"zero", func(v *agentv1.GetAgentNodeDispatchRequest) { v.Fence.ExecutionEpoch = 0 }, codes.InvalidArgument},
		{"empty-node", func(v *agentv1.GetAgentNodeDispatchRequest) { v.NodeId = "" }, codes.InvalidArgument},
		{"missing-action", func(v *agentv1.GetAgentNodeDispatchRequest) { v.Fence.ActionId = "missing-action" }, codes.NotFound},
		{"run", func(v *agentv1.GetAgentNodeDispatchRequest) { v.RunId = "another_run" }, codes.FailedPrecondition},
		{"future", func(v *agentv1.GetAgentNodeDispatchRequest) { v.Fence.ExecutionEpoch++ }, codes.FailedPrecondition},
		{"unknown", func(v *agentv1.GetAgentNodeDispatchRequest) { v.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 0x01}) }, codes.InvalidArgument},
		{"fence-unknown", func(v *agentv1.GetAgentNodeDispatchRequest) {
			v.Fence.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 0x01})
		}, codes.InvalidArgument},
	} {
		t.Run(test.name, func(t *testing.T) {
			bad := proto.Clone(query).(*agentv1.GetAgentNodeDispatchRequest)
			test.edit(bad)
			if _, err := nodes.GetDispatch(ctx, bad); status.Code(err) != test.want {
				t.Fatalf("invalid query accepted or wrong error: %v", err)
			}
		})
	}
}

func TestAgentNodeRPCShadowMissingStoreAndPlanning(t *testing.T) {
	shadow := &agentNodeService{run: &agentRunService{health: ShadowHealth()}}
	missing := &agentNodeService{run: &agentRunService{health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}}
	for _, test := range []struct {
		service *agentNodeService
		want    codes.Code
	}{{shadow, codes.PermissionDenied}, {missing, codes.Unavailable}} {
		if _, err := test.service.BeginDispatch(context.Background(), nil); status.Code(err) != test.want {
			t.Fatalf("process authority bypassed: %v", err)
		}
		if _, err := test.service.GetDispatch(context.Background(), nil); status.Code(err) != test.want {
			t.Fatalf("query process authority bypassed: %v", err)
		}
	}
	control := agentExecutionRPCStore(t, true)
	runs, nodes := agentNodeRPCClients(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	authority, err := runs.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	for index := int64(0); index < 2; index++ {
		if _, err := runs.AppendEvent(ctx, agentRPCAppend(index, authority.MetadataEpoch)); err != nil {
			t.Fatal(err)
		}
	}
	request := executionRPCInput(authority.MetadataEpoch)
	request.Phase, request.ExpectedIndex = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, 2
	claim, err := runs.ClaimExecution(ctx, request)
	if err != nil {
		t.Fatal(err)
	}
	result, err := nodes.BeginDispatch(ctx, &agentv1.BeginAgentNodeDispatchRequest{SchemaVersion: 1,
		OperationId: "rpc-plan-leader", Actor: request.Actor, Fence: claim.Claim.Fence, RunId: request.RunId,
		NodeId: "leader", Owner: claim.Claim.Owner, ClaimToken: claim.Claim.ClaimToken})
	if err != nil || !result.GetFresh() || result.GetIntent().GetPhase() != request.Phase || result.GetIntent().GetTask() != "plan agent run" {
		t.Fatalf("planning dispatch scope: %+v %v", result, err)
	}
}

func TestAgentNodeRPCDeniedDependencyDoesNotUseImportedProgress(t *testing.T) {
	control := agentExecutionRPCStore(t, true)
	runs, nodes := agentNodeRPCClients(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	authority, err := runs.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	for index := int64(0); index < 4; index++ {
		event := agentRPCAppend(index, authority.MetadataEpoch)
		if index == 2 {
			event.Event.Type, event.Event.Status, event.Event.HasPlan = "agent_plan", "", true
			event.Event.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}, {Id: "critic", Task: "review", DependsOn: []string{"coder"}}}
		}
		if index == 3 {
			event.Event.Status = "running"
		}
		if _, err := runs.AppendEvent(ctx, event); err != nil {
			t.Fatal(err)
		}
	}
	claim, err := runs.ClaimExecution(ctx, executionRPCInput(authority.MetadataEpoch))
	if err != nil {
		t.Fatal(err)
	}
	input := &agentv1.BeginAgentNodeDispatchRequest{SchemaVersion: 1, OperationId: "rpc-dependent-critic",
		Actor: "operator:agent-test", Fence: claim.Claim.Fence, RunId: claim.Claim.RunId,
		NodeId: "critic", Owner: claim.Claim.Owner, ClaimToken: claim.Claim.ClaimToken}
	if _, err := nodes.BeginDispatch(ctx, input); status.Code(err) != codes.FailedPrecondition || status.Convert(err).Message() != "AGENT_NODE_DEPENDENCY_UNVERIFIED" {
		t.Fatalf("dependency admitted without independent outcome: %v", err)
	}
	if _, found, err := control.AgentNodeDispatch(input.Fence.ActionId, input.NodeId); err != nil || found {
		t.Fatalf("denied dependency created a node intent: %v %v", found, err)
	}
}

func TestAgentNodeRPCQueryDeniesUnpromotedActionAndClosedStore(t *testing.T) {
	control := agentExecutionRPCStore(t, false)
	service := &agentNodeService{run: &agentRunService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}}
	query := &agentv1.GetAgentNodeDispatchRequest{Fence: &commonv1.ActionFence{ActionId: "unpromoted-action", ExecutionEpoch: 1}, RunId: "run_rpc", NodeId: "coder"}
	if _, err := service.GetDispatch(context.Background(), query); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("query treated shadow action as authoritative: %v", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := service.GetDispatch(context.Background(), query); status.Code(err) != codes.DataLoss {
		t.Fatalf("closed authority read: %v", err)
	}
}
