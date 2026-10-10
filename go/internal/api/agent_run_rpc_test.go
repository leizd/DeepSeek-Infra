package api

import (
	"encoding/json"
	"fmt"
	"net"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
)

// This fixture authorizes a fresh empty deployment only; it makes no claim
// about importing an existing Python Agent history or Rust artifact custody.
func agentRPCStore(t *testing.T) *store.Control {
	t.Helper()
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "agent-rpc-test", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionRoutePublic, FleetID: mutationFleetID, Environment: mutationEnvironment})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	checkpoint := authorityCheckpointFixture(t)
	if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
		t.Fatal(err)
	}
	for _, next := range []store.CutoverState{store.CutoverDualEvaluate, store.CutoverGoAuthoritative} {
		current, err := control.GetCutover("agent_run")
		if err != nil {
			t.Fatal(err)
		}
		request := signedPromotionRoute(t, store.CutoverTransition{Domain: "agent_run", To: next, ExpectedRevision: current.Revision,
			ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "agent-rpc-" + string(next), Authority: checkpoint}, current, 1000, nil)
		if _, err := control.TransitionCutover(request); err != nil {
			t.Fatal(err)
		}
	}
	return control
}

func agentRunRPCClient(t *testing.T, control *store.Control, health ControlPlaneHealth) agentv1.AgentRunControlClient {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	rpc := NewControlRPC(control, testInternalBearer, health)
	done := make(chan error, 1)
	go func() { done <- rpc.Serve(listener) }()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		rpc.Stop()
		<-done
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close(); rpc.Stop(); <-done })
	return agentv1.NewAgentRunControlClient(connection)
}

func agentRPCAppend(index int64, epoch uint64) *agentv1.AppendAgentRunEventRequest {
	input := &agentv1.AppendAgentRunEventRequest{SchemaVersion: 1, OperationId: fmt.Sprintf("rpc-event-%d", index), Actor: "operator:rpc-test",
		RunId: "run_rpc", ExpectedIndex: index, Fence: &commonv1.ActionFence{ActionId: "run_rpc", ExecutionEpoch: epoch},
		Event: &agentv1.AgentRunEventMetadata{Type: "run_status", Status: "planning",
			Body: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("a", 64), Length: 30}}}
	if index == 0 {
		input.Event.Status = "created"
		input.Create = &agentv1.AgentRunCreateMetadata{Preset: "full", ConfirmPlan: true,
			Request: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 100}}
	}
	return input
}

func TestAgentRunRPCRealAuthenticationPersistenceCursorAndFences(t *testing.T) {
	control := agentRPCStore(t)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	for _, bearer := range []string{"", "wrong"} {
		if _, err := client.GetAuthority(controlRPCContext(t, bearer), &agentv1.AgentRunAuthorityInput{}); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("unauthorized authority query: %v", err)
		}
	}
	authority, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil || !authority.Authoritative || authority.MetadataEpoch == 0 {
		t.Fatalf("durable authority: %v %v", authority, err)
	}
	input := agentRPCAppend(0, authority.MetadataEpoch)
	// Caller stamps are ignored; the authenticated control transaction owns them.
	input.Event.RunId, input.Event.Index, input.Event.CreatedAt = "forged", 999, "forged"
	input.Event.Fence = &commonv1.ActionFence{ActionId: "forged", ExecutionEpoch: authority.MetadataEpoch + 20}
	first, err := client.AppendEvent(ctx, input)
	if err != nil || first.AlreadyApplied || first.Event.Index != 0 || first.Run.NextIndex != 1 || first.Event.RunId != input.RunId ||
		first.Event.Fence.ActionId != input.RunId || first.Event.Fence.ExecutionEpoch != authority.MetadataEpoch || first.Event.CreatedAt != "1970-01-01T00:16:40Z" {
		t.Fatalf("real append: %v %v", first, err)
	}
	for _, request := range []*agentv1.AppendAgentRunEventRequest{agentRPCAppend(0, authority.MetadataEpoch), agentRPCAppend(0, authority.MetadataEpoch)} {
		response, err := client.AppendEvent(ctx, request)
		if err != nil || !response.AlreadyApplied || response.Event.Index != 0 || response.Run.NextIndex != 1 {
			t.Fatalf("idempotent append: %v %v", response, err)
		}
	}
	for _, fence := range []*commonv1.ActionFence{nil, {ActionId: "other", ExecutionEpoch: authority.MetadataEpoch},
		{ActionId: "run_rpc", ExecutionEpoch: 0}, {ActionId: "run_rpc", ExecutionEpoch: authority.MetadataEpoch + 1}} {
		request := agentRPCAppend(1, authority.MetadataEpoch)
		request.Fence = fence
		if _, err := client.AppendEvent(ctx, request); status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("forged fence accepted: %v", err)
		}
	}
	if _, err := client.AppendEvent(ctx, agentRPCAppend(1, authority.MetadataEpoch)); err != nil {
		t.Fatal(err)
	}
	plan := agentRPCAppend(2, authority.MetadataEpoch)
	plan.Event.Type, plan.Event.Status, plan.Event.HasPlan = "agent_plan", "", true
	plan.Event.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "code"}, {Id: "critic", Task: "review", DependsOn: []string{"coder"}}}
	if _, err := client.AppendEvent(ctx, plan); err != nil {
		t.Fatal(err)
	}
	readFence := &commonv1.ActionFence{ActionId: "run_rpc", ExecutionEpoch: authority.MetadataEpoch}
	page, err := client.EventsAfter(ctx, &agentv1.AgentRunEventsRequest{RunId: "run_rpc", After: 0, Limit: 1, Fence: readFence})
	if err != nil || !page.Authoritative || len(page.Events) != 1 || page.Events[0].Index != 1 {
		t.Fatalf("real cursor: %v %v", page, err)
	}
	detail, err := client.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: "run_rpc", Fence: readFence})
	if err != nil || !detail.Authoritative || detail.Run.NextIndex != 3 || len(detail.Run.Nodes) != 2 ||
		detail.Run.Nodes[0].Id != "coder" || detail.Run.Nodes[0].State != "queued" || detail.Run.Nodes[1].State != "created" {
		t.Fatalf("real DAG snapshot: %v %v", detail, err)
	}
	if _, exists, err := control.GetActionLease("run_rpc"); err != nil || exists {
		t.Fatalf("metadata RPC granted execution: %v %v", exists, err)
	}
	if _, err := client.EventsAfter(ctx, &agentv1.AgentRunEventsRequest{RunId: "run_rpc", After: -2, Limit: 1, Fence: readFence}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("invalid cursor accepted: %v", err)
	}
	if _, err := client.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: "missing", Fence: &commonv1.ActionFence{ActionId: "missing", ExecutionEpoch: authority.MetadataEpoch}}); status.Code(err) != codes.NotFound {
		t.Fatalf("missing run: %v", err)
	}
}

func TestAgentRunRPCShadowAndMissingStoreCannotGrantAuthority(t *testing.T) {
	control := agentRPCStore(t)
	for _, test := range []struct {
		name    string
		control *store.Control
		health  ControlPlaneHealth
		code    codes.Code
	}{
		{"shadow process", control, ShadowHealth(), codes.PermissionDenied},
		{"missing store", nil, HealthFor(config.Config{Mode: config.ModeAuthoritative}), codes.Unavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			client := agentRunRPCClient(t, test.control, test.health)
			ctx := controlRPCContext(t, testInternalBearer)
			if _, err := client.AppendEvent(ctx, agentRPCAppend(0, 1)); status.Code(err) != test.code {
				t.Fatalf("unavailable mutation: %v", err)
			}
			if test.control != nil {
				authority, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
				if err != nil || authority.Authoritative {
					t.Fatalf("shadow authority: %v %v", authority, err)
				}
			}
		})
	}
	// Process configuration alone cannot promote an unclaimed domain.
	unpromoted, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "unpromoted-agent-rpc"})
	if err != nil {
		t.Fatal(err)
	}
	defer unpromoted.Close()
	client := agentRunRPCClient(t, unpromoted, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	if authority, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{}); err != nil || authority.Authoritative {
		t.Fatalf("configuration supplied domain authority: %v %v", authority, err)
	}
	if _, err := client.AppendEvent(ctx, agentRPCAppend(0, 1)); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("unpromoted mutation: %v", err)
	}
}

func TestAgentRunRPCMalformedRequestsAndConflictDoNotChangeHistory(t *testing.T) {
	control := agentRPCStore(t)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	authority, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	input := agentRPCAppend(0, authority.MetadataEpoch)
	if _, err := client.AppendEvent(ctx, input); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name   string
		change func(*agentv1.AppendAgentRunEventRequest)
		code   codes.Code
	}{
		{"schema", func(r *agentv1.AppendAgentRunEventRequest) { r.SchemaVersion = 2 }, codes.InvalidArgument},
		{"missing event", func(r *agentv1.AppendAgentRunEventRequest) { r.Event = nil }, codes.InvalidArgument},
		{"unmarked plan", func(r *agentv1.AppendAgentRunEventRequest) { r.Event.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder"}} }, codes.InvalidArgument},
		{"actor", func(r *agentv1.AppendAgentRunEventRequest) { r.Actor = "" }, codes.InvalidArgument},
		{"operation", func(r *agentv1.AppendAgentRunEventRequest) { r.OperationId = "" }, codes.InvalidArgument},
		{"body digest", func(r *agentv1.AppendAgentRunEventRequest) { r.Event.Body.Sha256 = "bad" }, codes.InvalidArgument},
		{"body length", func(r *agentv1.AppendAgentRunEventRequest) { r.Event.Body.Length = 0 }, codes.InvalidArgument},
		{"duplicate create", func(r *agentv1.AppendAgentRunEventRequest) { r.Create = input.Create }, codes.InvalidArgument},
		{"stale cursor", func(r *agentv1.AppendAgentRunEventRequest) { r.ExpectedIndex = 0 }, codes.Aborted},
		{"replayed different request", func(r *agentv1.AppendAgentRunEventRequest) { r.OperationId = input.OperationId }, codes.Aborted},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := agentRPCAppend(1, authority.MetadataEpoch)
			test.change(request)
			if _, err := client.AppendEvent(ctx, request); status.Code(err) != test.code {
				t.Fatalf("invalid request: %v", err)
			}
		})
	}
	fence := &commonv1.ActionFence{ActionId: "run_rpc", ExecutionEpoch: authority.MetadataEpoch}
	for _, request := range []*agentv1.AgentRunEventsRequest{
		{RunId: "run_rpc", Fence: fence, After: -1, Limit: 0},
		{RunId: "run_rpc", Fence: fence, After: -1, Limit: 1025},
		{RunId: "../run", Fence: fence, After: -1, Limit: 1},
	} {
		if _, err := client.EventsAfter(ctx, request); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid page: %v", err)
		}
	}
	if _, err := client.GetRun(ctx, &agentv1.GetAgentRunRequest{}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("invalid run: %v", err)
	}
	if _, err := client.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: "run_rpc"}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("unfenced read: %v", err)
	}
	if _, err := client.EventsAfter(ctx, &agentv1.AgentRunEventsRequest{RunId: "run_rpc", Fence: &commonv1.ActionFence{ActionId: "run_rpc", ExecutionEpoch: authority.MetadataEpoch + 1}, After: -1, Limit: 1}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("future read: %v", err)
	}
	missingFence := &commonv1.ActionFence{ActionId: "missing", ExecutionEpoch: authority.MetadataEpoch}
	if _, err := client.EventsAfter(ctx, &agentv1.AgentRunEventsRequest{RunId: "missing", Fence: missingFence, After: -1, Limit: 1}); status.Code(err) != codes.NotFound {
		t.Fatalf("missing history: %v", err)
	}
	detail, err := client.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: "run_rpc", Fence: fence})
	page, pageErr := client.EventsAfter(ctx, &agentv1.AgentRunEventsRequest{RunId: "run_rpc", Fence: fence, After: -1, Limit: 1024})
	if err != nil || pageErr != nil || detail.Run.NextIndex != 1 || len(page.Events) != 1 {
		t.Fatalf("refusal changed history: %v %v %v %v", detail, page, err, pageErr)
	}
}

func TestAgentRunRPCPlanPresenceCountersAndWriterLoss(t *testing.T) {
	control := agentRPCStore(t)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	authority, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.AppendEvent(ctx, agentRPCAppend(0, authority.MetadataEpoch)); err != nil {
		t.Fatal(err)
	}
	appendEvent := func(index int64, event *agentv1.AgentRunEventMetadata) *agentv1.AppendAgentRunEventResponse {
		t.Helper()
		request := agentRPCAppend(index, authority.MetadataEpoch)
		event.Body = request.Event.Body
		request.Event = event
		result, err := client.AppendEvent(ctx, request)
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	defaults := appendEvent(1, &agentv1.AgentRunEventMetadata{Type: "agent_plan", HasPlan: true})
	if len(defaults.Run.Plan) != 4 {
		t.Fatal("explicit empty plan lost default semantics")
	}
	cleared := appendEvent(2, &agentv1.AgentRunEventMetadata{Type: "agent_plan"})
	if len(cleared.Run.Plan) != 0 || len(cleared.Run.Nodes) != 0 {
		t.Fatal("missing plan retained unused nodes")
	}
	duration := int64(25)
	output := appendEvent(3, &agentv1.AgentRunEventMetadata{Type: "agent_output", Phase: "coder", DurationMs: &duration, PromptTokens: 15, CompletionTokens: 9})
	if len(output.Run.Nodes) != 1 || output.Run.Nodes[0].PromptTokens != 15 || output.Run.Nodes[0].CompletionTokens != 9 || output.Run.Nodes[0].GetLatencyMs() != 25 {
		t.Fatalf("typed counters: %v", output)
	}
	reset := appendEvent(4, &agentv1.AgentRunEventMetadata{Type: "final_reset", Scope: "final_answer"})
	if reset.Run.FinalAfter != 4 {
		t.Fatal("final cursor lost")
	}
	fence := &commonv1.ActionFence{ActionId: "run_rpc", ExecutionEpoch: authority.MetadataEpoch}
	shadow := agentRunRPCClient(t, control, ShadowHealth())
	read, err := shadow.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: "run_rpc", Fence: fence})
	if err != nil || read.Authoritative {
		t.Fatalf("shadow read asserted authority: %v %v", read, err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{}); status.Code(err) != codes.DataLoss {
		t.Fatalf("closed authority: %v", err)
	}
	if _, err := client.AppendEvent(ctx, agentRPCAppend(5, authority.MetadataEpoch)); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("lost writer: %v", err)
	}
}

func TestAgentRunRPCLegacyReadAndUnavailableStoreAreExplicit(t *testing.T) {
	health := HealthFor(config.Config{Mode: config.ModeAuthoritative})
	missing := agentRunRPCClient(t, nil, health)
	ctx := controlRPCContext(t, testInternalBearer)
	fence := &commonv1.ActionFence{ActionId: "run_rpc", ExecutionEpoch: 1}
	if _, err := missing.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("missing authority: %v", err)
	}
	if _, err := missing.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: "run_rpc", Fence: fence}); status.Code(err) != codes.Unavailable {
		t.Fatalf("missing store read: %v", err)
	}
	if _, err := missing.EventsAfter(ctx, &agentv1.AgentRunEventsRequest{RunId: "run_rpc", After: -1, Limit: 1, Fence: fence}); status.Code(err) != codes.Unavailable {
		t.Fatalf("missing store events: %v", err)
	}
	control := agentRPCStore(t)
	current, err := control.GetCutover("agent_run")
	if err != nil {
		t.Fatal(err)
	}
	fence.ExecutionEpoch = uint64(current.Epoch)
	legacy := store.Record{Domain: "agent_run", ID: "run_rpc", State: "created", Revision: 1, ExecutionEpoch: uint64(current.Epoch), Payload: json.RawMessage(`{"legacy":true}`)}
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{OperationID: "legacy-rpc", ActionID: legacy.ID, Actor: "operator:legacy-test", Record: legacy}); err != nil {
		t.Fatal(err)
	}
	client := agentRunRPCClient(t, control, health)
	if _, err := client.GetRun(ctx, &agentv1.GetAgentRunRequest{RunId: "run_rpc", Fence: fence}); status.Code(err) != codes.DataLoss {
		t.Fatalf("unmigrated history read: %v", err)
	}
	if _, err := client.EventsAfter(ctx, &agentv1.AgentRunEventsRequest{RunId: "run_rpc", After: -1, Limit: 1, Fence: fence}); status.Code(err) != codes.DataLoss {
		t.Fatalf("unmigrated history events: %v", err)
	}
}
