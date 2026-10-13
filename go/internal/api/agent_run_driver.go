package api

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sort"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

var (
	errAgentDriverCustody = errors.New("AGENT_DRIVER_CUSTODY_UNAVAILABLE")
	errAgentDriverUnknown = errors.New("AGENT_DRIVER_EFFECT_UNCONFIRMED")
	errAgentDriverTools   = errors.New("AGENT_DRIVER_TOOLS_PENDING")
)

const agentDriverLease = 120
const agentDriverActor = "operator:agent-driver"

// Body bytes, credentials, private keys and tool effects remain in Rust. This
// controller uses only the existing typed worker/control APIs and signed proofs.
type agentDriverWorker interface {
	ExecuteAgentPlan(context.Context, *agentv1.ExecuteAgentPlanRequest) (*agentv1.AgentNodeEffectMetadata, error)
	ExecuteAgentTask(context.Context, *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error)
	ExecuteAgentSynthesis(context.Context, *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error)
	QueryAgentNodeEffect(context.Context, *agentv1.QueryAgentNodeEffectRequest) (*agentv1.AgentNodeEffectMetadata, error)
	ProjectAgentPlan(context.Context, *agentv1.ProjectAgentPlanRequest) (*agentv1.AgentPlanProjectionReceipt, error)
	ProjectAgentSynthesis(context.Context, *agentv1.ProjectAgentSynthesisRequest) (*agentv1.AgentSynthesisProjectionReceipt, error)
}

type agentRunDriver struct {
	service   *agentRunService
	owner     string
	sequence  uint64
	installed map[string]bool
}

func driverOperation(parts ...any) string {
	values := make([]string, len(parts))
	for index, part := range parts {
		values[index] = fmt.Sprintf("%T:%v", part, part)
	}
	raw, _ := json.Marshal(values)
	digest := sha256.Sum256(raw)
	return "agent-driver-" + hex.EncodeToString(digest[:])
}

// StartAgentRunDriver is part of the authoritative deepseekd lifecycle. It is
// absent in shadow mode and drains before the shared control store is closed.
func StartAgentRunDriver(ctx context.Context, control *store.Control, health ControlPlaneHealth) <-chan struct{} {
	done := make(chan struct{})
	service := &agentRunService{control: control, health: health}
	if control == nil || !service.processAuthoritative() {
		close(done)
		return done
	}
	var identity [16]byte
	if _, err := rand.Read(identity[:]); err != nil {
		log.Printf("agent driver identity unavailable")
		close(done)
		return done
	}
	driver := &agentRunDriver{service: service, owner: "agent-driver-" + hex.EncodeToString(identity[:]), installed: make(map[string]bool)}
	go func() {
		defer close(done)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		prior := make(map[string]string)
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				records, err := control.ListAuthoritativeRecords("agent_run")
				if err != nil {
					continue // No domain promotion or empty-success substitution.
				}
				live := make(map[string]bool)
				for _, record := range records {
					if ctx.Err() != nil {
						return
					}
					run, found, err := control.AgentRunMetadata(record.ID)
					if err != nil || !found || !driverRunnable(run) {
						continue
					}
					live[run.RunID] = true
					err = driver.drive(ctx, run)
					if err == nil {
						delete(prior, run.RunID)
					} else if prior[run.RunID] != err.Error() {
						// Never log bodies, grants, bearer tokens or claim tokens.
						log.Printf("agent driver run=%s waiting: %s", run.RunID, driverErrorCode(err))
						prior[run.RunID] = err.Error()
					}
				}
				for id := range prior {
					if !live[id] {
						delete(prior, id)
					}
				}
			}
		}
	}()
	return done
}

func driverErrorCode(err error) string {
	switch {
	case errors.Is(err, errAgentDriverUnknown):
		return errAgentDriverUnknown.Error()
	case errors.Is(err, errAgentDriverTools):
		return errAgentDriverTools.Error()
	case errors.Is(err, context.Canceled):
		return "AGENT_DRIVER_CANCELLED"
	default:
		return "AGENT_DRIVER_AUTHORITY_OR_TRANSPORT_UNAVAILABLE"
	}
}

func driverRunnable(run agent.RunMetadata) bool {
	return !run.Cancelled && (run.Status == "created" || run.Status == "planning" || run.Status == "running")
}

func driverTasksComplete(run agent.RunMetadata) bool {
	if len(run.Plan) == 0 {
		return false
	}
	nodes := run.Nodes()
	for _, entry := range run.Plan {
		if nodes[entry.ID].State != "succeeded" || nodes[entry.ID].Failed {
			return false
		}
	}
	return true
}

func (driver *agentRunDriver) phase(run agent.RunMetadata) (agentv1.AgentRunExecutionPhase, *store.Record, error) {
	phase := agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN
	if run.Status == "running" {
		phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil {
		return phase, nil, err
	}
	var current *store.Record
	var completedTasks bool
	var newest int64 = -1
	planBytes, _ := json.Marshal(run.Plan)
	planHash := sha256.Sum256(planBytes)
	planDigest := hex.EncodeToString(planHash[:])
	for _, record := range records {
		binding, err := store.AgentRunExecutionBindingFromRecord(record)
		if err != nil || binding.RunID != run.RunID {
			continue
		}
		matching := binding.Request == run.Request && binding.PlanDigest == planDigest && binding.MetadataIndex <= run.NextIndex
		if !matching && binding.Phase == executionPhaseName(phase) && record.State != "SUCCEEDED" {
			return phase, nil, errAgentDriverUnknown
		}
		if !matching {
			continue
		}
		if binding.Phase == "tasks" && record.State == "SUCCEEDED" && driverTasksComplete(run) {
			completedTasks = true
		}
		if binding.Phase == executionPhaseName(phase) && record.State != "SUCCEEDED" && binding.MetadataIndex > newest {
			copy := record
			current, newest = &copy, binding.MetadataIndex
		}
	}
	if phase == agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS && current == nil && completedTasks {
		phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS
		// Preserve an admitted synthesis scope on retry/restart as well.
		for _, record := range records {
			binding, err := store.AgentRunExecutionBindingFromRecord(record)
			if err == nil && binding.RunID == run.RunID && binding.Request == run.Request && binding.PlanDigest == planDigest && binding.Phase == "synthesis" &&
				record.State != "SUCCEEDED" && binding.MetadataIndex <= run.NextIndex && binding.MetadataIndex > newest {
				copy := record
				current, newest = &copy, binding.MetadataIndex
			}
		}
	}
	return phase, current, nil
}

func (driver *agentRunDriver) admission(ctx context.Context, run agent.RunMetadata, phase agentv1.AgentRunExecutionPhase, existing *store.Record) (*agentv1.AgentRunExecutionClaim, error) {
	if existing != nil {
		if existing.State == "EFFECT_UNKNOWN" {
			return nil, errAgentDriverUnknown
		}
		lease, found, err := driver.service.control.GetActionLease(existing.ID)
		if err != nil {
			return nil, err
		}
		if found && lease.Owner == driver.owner {
			renewed, err := driver.service.RenewExecution(ctx, &agentv1.RenewAgentRunExecutionRequest{
				Fence: &commonv1.ActionFence{ActionId: existing.ID, ExecutionEpoch: uint64(lease.Epoch)}, RunId: run.RunID,
				Owner: lease.Owner, ClaimToken: lease.ClaimToken, LeaseSeconds: agentDriverLease})
			if err == nil {
				return renewed.Claim, nil
			}
		}
		// Generic native admission preserves the original scope on takeover and
		// enters RECONCILING. It cannot authorize dispatch of an uncertain node.
		admitted, err := driver.service.control.AdmitAndClaimAction(store.AdmissionRequest{
			ActionID: existing.ID, Owner: driver.owner, LeaseSeconds: agentDriverLease})
		if err != nil {
			return nil, err
		}
		return executionClaimProtocol(admitted, driver.service.control.Writer().LeaseUntil)
	}
	authority, err := driver.service.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil || !authority.GetAuthoritative() {
		if err == nil {
			err = errAgentDriverCustody
		}
		return nil, err
	}
	input := &agentv1.ClaimAgentRunExecutionRequest{SchemaVersion: 1, OperationId: driverOperation("claim:", driver.owner, ":", run.RunID, ":", phase, ":", run.NextIndex),
		Actor: agentDriverActor, RunId: run.RunID, Phase: phase, ExpectedIndex: run.NextIndex,
		MetadataFence: &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch}, Owner: driver.owner, LeaseSeconds: agentDriverLease}
	claimed, err := driver.service.ClaimExecution(ctx, input)
	if err != nil {
		return nil, err
	}
	return claimed.Claim, nil
}

func (driver *agentRunDriver) grant(ctx context.Context, claim *agentv1.AgentRunExecutionClaim) (*agentv1.AgentRunExecutionClaim, []byte, error) {
	if claim.ReconciliationRequired {
		return claim, nil, nil
	}
	renewed, err := driver.service.RenewExecution(ctx, &agentv1.RenewAgentRunExecutionRequest{Fence: claim.Fence,
		RunId: claim.RunId, Owner: claim.Owner, ClaimToken: claim.ClaimToken, LeaseSeconds: agentDriverLease})
	if err != nil {
		return nil, nil, err
	}
	claim = renewed.Claim
	identity := driverOperation("installed:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch)
	if driver.installed[identity] {
		driver.sequence++
		refreshed, err := driver.service.RefreshAuthorization(ctx, &agentv1.RefreshAgentRunAuthorizationRequest{
			SchemaVersion: 1, OperationId: driverOperation("refresh:", driver.owner, ":", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch, ":", driver.sequence),
			Fence: claim.Fence, RunId: claim.RunId, Owner: claim.Owner, ClaimToken: claim.ClaimToken, LeaseSeconds: agentDriverLease})
		if err != nil {
			return nil, nil, err
		}
		return refreshed.Claim, refreshed.CanonicalAuthorization, nil
	}
	factory := driver.service.executionSigner
	if factory == nil {
		factory = configuredAgentExecutionSigner
	}
	session, err := factory(driver.service.control)
	if err != nil {
		return nil, nil, err
	}
	if session == nil {
		return nil, nil, errAgentDriverCustody
	}
	if session.close != nil {
		defer session.close()
	}
	if session.client == nil {
		return nil, nil, errAgentDriverCustody
	}
	intent, err := agentSigningIntent(claim)
	if err != nil {
		return nil, nil, err
	}
	// The first phase authorization installs its admitted epoch. Later nodes
	// refresh explicitly without replaying epoch installation or expired dates.
	requestID, nonce := agentSigningIdentifiers(intent)
	raw, err := session.client.AuthorizeAgentExecution(ctx, claim, requestID, nonce, session.binding)
	if err != nil {
		return nil, nil, err
	}
	if _, err := verifiedAgentAuthorization(session, intent, requestID, nonce, raw, claim, true); err != nil {
		return nil, nil, err
	}
	if driver.installed == nil {
		driver.installed = make(map[string]bool)
	}
	driver.installed[identity] = true
	return claim, raw, nil
}

func (driver *agentRunDriver) heartbeat(ctx context.Context, claim *agentv1.AgentRunExecutionClaim, cancel context.CancelFunc) <-chan struct{} {
	done := make(chan struct{})
	stable := proto.Clone(claim).(*agentv1.AgentRunExecutionClaim)
	go func() {
		defer close(done)
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if _, err := driver.service.RenewExecution(ctx, &agentv1.RenewAgentRunExecutionRequest{Fence: stable.Fence,
					RunId: stable.RunId, Owner: stable.Owner, ClaimToken: stable.ClaimToken, LeaseSeconds: agentDriverLease}); err != nil {
					cancel()
					return
				}
			}
		}
	}()
	return done
}

func (driver *agentRunDriver) drive(ctx context.Context, run agent.RunMetadata) error {
	phase, existing, err := driver.phase(run)
	if err != nil {
		return err
	}
	factory := driver.service.executionSigner
	if factory == nil {
		factory = configuredAgentExecutionSigner
	}
	session, err := factory(driver.service.control)
	if err != nil {
		return err
	}
	if session == nil {
		return errAgentDriverCustody
	}
	if session.close != nil {
		defer session.close()
	}
	if session.client == nil {
		return errAgentDriverCustody
	}
	client, ok := session.client.(agentDriverWorker)
	if !ok {
		return errAgentDriverCustody
	}
	claim, err := driver.admission(ctx, run, phase, existing)
	if err != nil {
		return err
	}
	phaseCtx, cancel := context.WithCancel(ctx)
	heartbeat := driver.heartbeat(phaseCtx, claim, cancel)
	defer func() { cancel(); <-heartbeat }()
	claim, grant, err := driver.grant(phaseCtx, claim)
	if err != nil {
		return err
	}
	nodes := &agentNodeService{run: driver.service}
	switch phase {
	case agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN:
		effect, err := driver.node(phaseCtx, client, nodes, claim, grant, "leader")
		if err != nil {
			return err
		}
		candidates := make([]agent.RunPlanNode, 0)
		for _, node := range effect.OutcomeReceipt.Evidence.ProviderPlan {
			candidates = append(candidates, agent.RunPlanNode{ID: node.Id, Task: node.Task, DependsOn: append([]string(nil), node.DependsOn...)})
		}
		next := "running"
		if run.ConfirmPlan {
			next = "awaiting_plan"
		}
		projection, err := client.ProjectAgentPlan(phaseCtx, &agentv1.ProjectAgentPlanRequest{SchemaVersion: 1,
			Source: driverQuery(effect), SettlementFence: claim.Fence, SettlementWriterToken: claim.WriterFencingToken,
			MetadataEpoch: claim.MetadataEpoch, ExpectedIndex: run.NextIndex, NormalizedPlan: planProtocol(agent.NormalizeRunPlan(candidates)),
			NextStatus: next, PlanningRequired: run.Status == "created"})
		if err != nil {
			return err
		}
		_, err = nodes.SettlePlan(phaseCtx, &agentv1.SettleAgentPlanRequest{SchemaVersion: 1,
			OperationId: driverOperation("settle-plan:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch), Actor: agentDriverActor,
			Fence: claim.Fence, RunId: run.RunID, Owner: claim.Owner, ClaimToken: claim.ClaimToken, Projection: projection})
		return err
	case agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS:
		return driver.tasks(phaseCtx, client, nodes, claim, grant, run)
	case agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS:
		effect, err := driver.node(phaseCtx, client, nodes, claim, grant, "synthesizer")
		if err != nil {
			return err
		}
		projection, err := client.ProjectAgentSynthesis(phaseCtx, &agentv1.ProjectAgentSynthesisRequest{SchemaVersion: 1,
			Source: driverQuery(effect), SettlementFence: claim.Fence, SettlementWriterToken: claim.WriterFencingToken,
			MetadataEpoch: claim.MetadataEpoch, ExpectedIndex: run.NextIndex})
		if err != nil {
			return err
		}
		_, err = nodes.SettleSynthesis(phaseCtx, &agentv1.SettleAgentSynthesisRequest{SchemaVersion: 1,
			OperationId: driverOperation("settle-synthesis:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch), Actor: agentDriverActor,
			Fence: claim.Fence, RunId: run.RunID, Owner: claim.Owner, ClaimToken: claim.ClaimToken, Projection: projection})
		return err
	default:
		return errAgentDriverCustody
	}
}

func driverQuery(effect *agentv1.AgentNodeEffectMetadata) *agentv1.QueryAgentNodeEffectRequest {
	return &agentv1.QueryAgentNodeEffectRequest{OperationId: effect.OperationId, Fence: effect.Fence, RunId: effect.RunId, NodeId: effect.NodeId}
}

func (driver *agentRunDriver) node(ctx context.Context, client agentDriverWorker, nodes *agentNodeService, claim *agentv1.AgentRunExecutionClaim, grant []byte, id string) (*agentv1.AgentNodeEffectMetadata, error) {
	prior, err := nodes.GetDispatch(ctx, &agentv1.GetAgentNodeDispatchRequest{Fence: claim.Fence, RunId: claim.RunId, NodeId: id})
	if err != nil {
		return nil, err
	}
	var effect *agentv1.AgentNodeEffectMetadata
	if prior.Found {
		// Only actual retained Rust custody can resolve a committed reservation.
		// An absent response or lost connection never grants a second call.
		effect, err = client.QueryAgentNodeEffect(ctx, &agentv1.QueryAgentNodeEffectRequest{OperationId: prior.Intent.OperationId,
			Fence: prior.Intent.Fence, RunId: claim.RunId, NodeId: id})
	} else if claim.ReconciliationRequired {
		return nil, errAgentDriverUnknown
	} else {
		operation := driverOperation("execute:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch, ":", id)
		if id == "leader" {
			effect, err = client.ExecuteAgentPlan(ctx, &agentv1.ExecuteAgentPlanRequest{SchemaVersion: 1, OperationId: operation,
				Actor: "rust-agent-worker", Claim: claim, SignedPhaseGrant: grant, Fence: claim.Fence})
		} else {
			input := &agentv1.ExecuteAgentTaskRequest{SchemaVersion: 1, OperationId: operation, Actor: "rust-agent-worker",
				Claim: claim, SignedPhaseGrant: grant, Fence: claim.Fence, NodeId: id}
			if id == "synthesizer" {
				effect, err = client.ExecuteAgentSynthesis(ctx, input)
			} else {
				effect, err = client.ExecuteAgentTask(ctx, input)
			}
		}
	}
	if err != nil {
		return nil, err
	}
	if effect == nil {
		return nil, errAgentDriverUnknown
	}
	if effect.State == "AWAITING_TOOLS" {
		return nil, errAgentDriverTools
	}
	if effect.State != "CONFIRMED" || effect.OutcomeReceipt == nil || effect.OutcomeReceipt.Evidence == nil {
		return nil, errAgentDriverUnknown
	}
	effect = proto.Clone(effect).(*agentv1.AgentNodeEffectMetadata)
	_, err = nodes.RecordOutcome(ctx, &agentv1.RecordAgentNodeOutcomeRequest{SchemaVersion: 1,
		OperationId: driverOperation("outcome:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch, ":", id), Actor: agentDriverActor,
		Fence: claim.Fence, RunId: claim.RunId, NodeId: id, OutcomeReceipt: effect.OutcomeReceipt})
	if err != nil {
		return nil, err
	}
	return effect, nil
}

func (driver *agentRunDriver) tasks(ctx context.Context, client agentDriverWorker, nodes *agentNodeService, claim *agentv1.AgentRunExecutionClaim, grant []byte, run agent.RunMetadata) error {
	for !driverTasksComplete(run) {
		entries := append([]agent.RunPlanNode(nil), run.Plan...)
		sort.Slice(entries, func(i, j int) bool { return entries[i].ID < entries[j].ID })
		ready := ""
		states := run.Nodes()
		for _, entry := range entries {
			if states[entry.ID].State == "succeeded" && !states[entry.ID].Failed {
				continue
			}
			if states[entry.ID].State == "failed" || states[entry.ID].Failed {
				return errAgentDriverUnknown
			}
			dependencies := true
			for _, dependency := range entry.DependsOn {
				if states[dependency].State != "succeeded" || states[dependency].Failed {
					dependencies = false
					break
				}
			}
			if dependencies {
				ready = entry.ID
				break
			}
		}
		if ready == "" {
			return errAgentDriverUnknown
		}
		var err error
		claim, grant, err = driver.grant(ctx, claim)
		if err != nil {
			return err
		}
		effect, err := driver.node(ctx, client, nodes, claim, grant, ready)
		if err != nil {
			return err
		}
		raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(effect.OutcomeReceipt)
		if err != nil {
			return err
		}
		digest := sha256.Sum256(raw)
		settled, err := nodes.SettleTask(ctx, &agentv1.SettleAgentTaskRequest{SchemaVersion: 1,
			OperationId: driverOperation("settle-task:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch, ":", ready), Actor: agentDriverActor,
			Fence: claim.Fence, RunId: run.RunID, NodeId: ready, Owner: claim.Owner, ClaimToken: claim.ClaimToken,
			ExpectedIndex: run.NextIndex, SourceReceiptSha256: hex.EncodeToString(digest[:])})
		if err != nil {
			return err
		}
		run, _, err = driver.service.control.AgentRunMetadata(settled.Run.RunId)
		if err != nil {
			return err
		}
	}
	_, err := nodes.CompleteTasks(ctx, &agentv1.CompleteAgentTasksRequest{SchemaVersion: 1,
		OperationId: driverOperation("complete-tasks:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch), Actor: agentDriverActor,
		Fence: claim.Fence, RunId: run.RunID, Owner: claim.Owner, ClaimToken: claim.ClaimToken, ExpectedIndex: run.NextIndex})
	return err
}
