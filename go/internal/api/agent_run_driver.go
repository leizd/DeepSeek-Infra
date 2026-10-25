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
	errAgentDriverReset   = errors.New("AGENT_RUNTIME_RESET_REQUIRES_GENERATION")
	errAgentDriverHeld    = errors.New("AGENT_RUNTIME_SUCCESSOR_HELD")
	errAgentDriverStale   = errors.New("STALE_EXECUTION_EPOCH")
	errAgentDriverFence   = errors.New("FENCE_MISMATCH")
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
		driver.run(ctx)
	}()
	return done
}

// run is the same private loop started by the authoritative lifecycle.
func (driver *agentRunDriver) run(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	prior := make(map[string]string)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			records, err := driver.service.control.ListAuthoritativeRecords("agent_run")
			if err != nil {
				continue // No domain promotion or empty-success substitution.
			}
			live := make(map[string]bool)
			for _, record := range records {
				if ctx.Err() != nil {
					return
				}
				run, found, err := driver.service.control.AgentRunMetadata(record.ID)
				if err != nil || !found || !driverRunnable(run) && !run.Cancelled {
					continue
				}
				live[run.RunID] = true
				if run.Cancelled {
					err = driver.recoverCancellation(ctx, run)
				} else {
					err = driver.drive(ctx, run)
				}
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
}

func driverErrorCode(err error) string {
	switch {
	case errors.Is(err, errAgentDriverUnknown):
		return errAgentDriverUnknown.Error()
	case errors.Is(err, errAgentDriverTools):
		return errAgentDriverTools.Error()
	case errors.Is(err, errAgentDriverReset):
		return errAgentDriverReset.Error()
	case errors.Is(err, errAgentDriverHeld):
		return errAgentDriverHeld.Error()
	case errors.Is(err, errAgentDriverStale):
		return errAgentDriverStale.Error()
	case errors.Is(err, errAgentDriverFence):
		return errAgentDriverFence.Error()
	case errors.Is(err, context.Canceled):
		return "AGENT_DRIVER_CANCELLED"
	default:
		var refused *store.AgentSuccessorError
		if errors.As(err, &refused) && refused.Code != "" {
			return refused.Code
		}
		return "AGENT_DRIVER_AUTHORITY_OR_TRANSPORT_UNAVAILABLE"
	}
}

func successorAdmissionError(code string) error {
	switch code {
	case "AGENT_DRIVER_TOOLS_PENDING":
		return errAgentDriverTools
	case "EFFECT_UNKNOWN":
		return errAgentDriverUnknown
	case "AGENT_RUNTIME_RESET_REQUIRES_GENERATION":
		return errAgentDriverReset
	case "AGENT_RUNTIME_SUCCESSOR_HELD":
		return errAgentDriverHeld
	case "STALE_EXECUTION_EPOCH":
		return errAgentDriverStale
	case "FENCE_MISMATCH":
		return errAgentDriverFence
	default:
		return errAgentDriverUnknown
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
		effect, err := driver.node(phaseCtx, client, nodes, claim, grant, "leader", "")
		if err != nil {
			return err
		}
		// A published answer ends the tool wait and carries no outcome receipt.
		if effect.State == "AWAITING_TOOLS" || effect.State == "ANSWER_PUBLISHED" {
			return nil
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
		effect, err := driver.node(phaseCtx, client, nodes, claim, grant, "synthesizer", "")
		if err != nil {
			return err
		}
		// A published answer ends the tool wait and carries no outcome receipt.
		if effect.State == "AWAITING_TOOLS" || effect.State == "ANSWER_PUBLISHED" {
			return nil
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

func (driver *agentRunDriver) node(ctx context.Context, client agentDriverWorker, nodes *agentNodeService, claim *agentv1.AgentRunExecutionClaim, grant []byte, id, nodeState string) (*agentv1.AgentNodeEffectMetadata, error) {
	prior, err := nodes.GetDispatch(ctx, &agentv1.GetAgentNodeDispatchRequest{Fence: claim.Fence, RunId: claim.RunId, NodeId: id})
	if err != nil {
		return nil, err
	}
	var effect *agentv1.AgentNodeEffectMetadata
	if prior.Found {
		// Only actual retained Rust custody can resolve a committed reservation.
		// An absent response or lost connection never grants a second call.
		if prior.Intent == nil || prior.Intent.OperationId == "" || claim.Fence == nil {
			return nil, errAgentDriverUnknown
		}
		effect, err = client.QueryAgentNodeEffect(ctx, &agentv1.QueryAgentNodeEffectRequest{OperationId: prior.Intent.OperationId,
			Fence: prior.Intent.Fence, RunId: claim.RunId, NodeId: id})
		if projectErr := driver.projectJournalObservation(claim.RunId, id, effect, err); projectErr != nil {
			return nil, projectErr
		}
		if err != nil {
			return nil, err
		}
		if effect == nil {
			return nil, errAgentDriverUnknown
		}
		if nodeState == "retrying" {
			// The old operation id stays the query key. A newer claim epoch is
			// the only fence that may run the stored successor.
			if prior.Intent.Fence != nil && claim.Fence.ExecutionEpoch > prior.Intent.Fence.ExecutionEpoch {
				return driver.successorExecute(ctx, client, nodes, claim, grant, id, prior.Intent.OperationId, effect.State)
			}
			return nil, driver.successorHold(nodes, claim, id, prior.Intent.OperationId, effect.State)
		}
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
	if code, fresh := agent.DispatchAllowed("live", "match", effect.State); !fresh {
		if code == "AGENT_DRIVER_TOOLS_PENDING" || effect.State == "AWAITING_TOOLS" {
			decision, observeErr := driver.retainedDriverToolResults(claim, effect)
			if observeErr != nil {
				return nil, observeErr
			}
			if !decision.Returned || len(decision.Events) == 0 {
				return nil, errAgentDriverTools
			}
			return effect, nil
		}
		// A published answer ends the tool wait. Recording its receipt stays
		// outside this branch.
		if effect.State == "ANSWER_PUBLISHED" {
			return effect, nil
		}
		return nil, errAgentDriverUnknown
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

// resetSharingFence reports the latest agent_reset for this node. A truncated
// history returns an error so a partial scan cannot mint. The metadata writer
// stamps actionId with the run id and executionEpoch with the agent_run
// cutover epoch; an event that already carries this action fence is accepted
// too. A missing event is not admitted.
func resetSharingFence(control *store.Control, runID, nodeID, actionID string, epoch uint64) (int64, bool, error) {
	if control == nil {
		return 0, false, nil
	}
	record, present, err := control.Get("agent_run", runID)
	if err != nil {
		return 0, false, err
	}
	metadataEpoch := uint64(0)
	if present {
		metadataEpoch = record.ExecutionEpoch
	}
	after := int64(-1)
	matched := false
	var index int64
	for page := 0; page < 8; page++ {
		batch, err := control.AgentRunEventsAfter(runID, after, 1024)
		if err != nil {
			return 0, false, err
		}
		if len(batch) == 0 {
			return index, matched, nil
		}
		for _, event := range batch {
			if event.Type != "agent_reset" || event.Phase != nodeID || event.RunID != runID {
				continue
			}
			sameAction := event.ActionID == actionID && event.ExecutionEpoch == epoch
			sameMetadata := metadataEpoch > 0 && event.ActionID == runID && event.ExecutionEpoch == metadataEpoch
			if sameAction || sameMetadata {
				matched = true
				index = event.Index
			}
		}
		if len(batch) < 1024 {
			return index, matched, nil
		}
		next := batch[len(batch)-1].Index
		if next <= after {
			return 0, false, errAgentDriverUnknown
		}
		after = next
	}
	return 0, false, errAgentDriverUnknown
}

// retainedDriverToolResults reads scheduling headers the driver already
// stores. Result bytes stay in Rust. A read error is not an unapplied effect.
func (driver *agentRunDriver) retainedDriverToolResults(
	claim *agentv1.AgentRunExecutionClaim,
	effect *agentv1.AgentNodeEffectMetadata,
) (agent.DriverToolDecision, error) {
	public := agent.ArtifactRef{}
	state := ""
	if effect != nil {
		state = effect.State
		if effect.PublicEvent != nil {
			public = agent.ArtifactRef{SHA256: effect.PublicEvent.Sha256, Length: effect.PublicEvent.Length}
		}
	}
	if driver == nil || driver.service == nil || driver.service.control == nil || claim == nil {
		return agent.RetainedDriverToolResults(state, public, nil), nil
	}
	events, err := driver.retainedPublicEvents(claim.RunId)
	if err != nil {
		return agent.DriverToolDecision{}, err
	}
	return agent.RetainedDriverToolResults(state, public, events), nil
}

func (driver *agentRunDriver) retainedPublicEvents(runID string) ([]agent.RunEvent, error) {
	control := driver.service.control
	after := int64(-1)
	events := []agent.RunEvent{}
	for page := 0; page < 8; page++ {
		batch, err := control.AgentRunEventsAfter(runID, after, 1024)
		if err != nil {
			return nil, err
		}
		if len(batch) == 0 {
			return events, nil
		}
		events = append(events, batch...)
		if len(batch) < 1024 {
			return events, nil
		}
		next := batch[len(batch)-1].Index
		if next <= after {
			return nil, errAgentDriverUnknown
		}
		after = next
	}
	return nil, errAgentDriverUnknown
}

// projectJournalObservation records a journal read on the public stream.
// A missing row, a timeout, and an absent log stay observations. A settled
// read is not appended. Absent control keeps the caller's error.
func (driver *agentRunDriver) projectJournalObservation(
	runID, nodeID string,
	effect *agentv1.AgentNodeEffectMetadata,
	queryErr error,
) error {
	if driver == nil || driver.service == nil || driver.service.control == nil {
		return nil
	}
	found := effect != nil
	state := ""
	if found {
		state = effect.State
	}
	decision := agent.ProjectJournalObservation(nodeID, found, state, queryErr)
	if !decision.Append {
		return nil
	}
	return driver.appendJournalObservation(runID, decision)
}

func (driver *agentRunDriver) appendJournalObservation(runID string, decision agent.JournalObservation) error {
	control := driver.service.control
	record, present, err := control.Get("agent_run", runID)
	if err != nil {
		return err
	}
	if !present || record.ExecutionEpoch == 0 {
		return errAgentDriverUnknown
	}
	run, found, err := control.AgentRunMetadata(runID)
	if err != nil || !found {
		if err != nil {
			return err
		}
		return errAgentDriverUnknown
	}
	same, err := journalObservationAlreadyPublic(control, runID, decision)
	if err != nil || same {
		return err
	}
	_, err = control.AppendAgentRunEvent(store.AgentRunEventMutation{
		OperationID:   driverOperation("project-journal:", runID, ":", decision.Phase, ":", decision.Status, ":", decision.Scope),
		Actor:         agentDriverActor,
		RunID:         runID,
		ExpectedIndex: run.NextIndex,
		Fence:         &commonv1.ActionFence{ActionId: runID, ExecutionEpoch: record.ExecutionEpoch},
		Event: agent.RunEvent{
			Type:   "node_effect",
			Phase:  decision.Phase,
			Status: decision.Status,
			Scope:  decision.Scope,
			Body:   agent.ArtifactRef{SHA256: decision.SHA256, Length: decision.Length},
		},
	})
	return err
}

func journalObservationAlreadyPublic(control *store.Control, runID string, decision agent.JournalObservation) (bool, error) {
	after := int64(-1)
	var latest agent.RunEvent
	seen := false
	for page := 0; page < 8; page++ {
		batch, err := control.AgentRunEventsAfter(runID, after, 1024)
		if err != nil {
			return false, err
		}
		if len(batch) == 0 {
			return journalObservationMatches(seen, latest, decision), nil
		}
		for _, event := range batch {
			if event.Type == "node_effect" && event.Phase == decision.Phase {
				latest = event
				seen = true
			}
		}
		if len(batch) < 1024 {
			return journalObservationMatches(seen, latest, decision), nil
		}
		next := batch[len(batch)-1].Index
		if next <= after {
			return false, errAgentDriverUnknown
		}
		after = next
	}
	return false, errAgentDriverUnknown
}

func journalObservationMatches(seen bool, latest agent.RunEvent, decision agent.JournalObservation) bool {
	return seen && latest.Status == decision.Status && latest.Scope == decision.Scope &&
		latest.Body.SHA256 == decision.SHA256 && latest.Body.Length == decision.Length
}

// successorHold stores the minted id beside the original operation and does
// not call the worker. This fence still has one effect.
func (driver *agentRunDriver) successorHold(nodes *agentNodeService, claim *agentv1.AgentRunExecutionClaim, nodeID, operationID, effectState string) error {
	resetAdmitted := false
	var resetIndex int64
	var control *store.Control
	if nodes != nil && nodes.run != nil {
		control = nodes.run.control
	}
	actionID := ""
	var epoch uint64
	if claim != nil && claim.Fence != nil {
		actionID = claim.Fence.ActionId
		epoch = claim.Fence.ExecutionEpoch
	}
	if control != nil && claim != nil && claim.Fence != nil {
		index, ok, err := resetSharingFence(control, claim.RunId, nodeID, actionID, epoch)
		if err != nil {
			return err
		}
		resetAdmitted = ok
		resetIndex = index
	}
	existing := ""
	if control != nil && actionID != "" {
		_, stored, found, err := control.AgentNodeDispatchSnapshot(actionID, nodeID)
		if err != nil {
			return err
		}
		if found && stored.OperationID == operationID {
			existing = stored.SuccessorOperationID
		}
	}
	decision := agent.SuccessorGeneration(agent.SuccessorInput{
		IdentityValid: true, Lease: "live", Epoch: "match", Effect: effectState,
		ResetAdmitted: resetAdmitted, ResetFenceMatches: resetAdmitted, OperationID: operationID,
		ActionID: actionID, ExecutionEpoch: epoch, NodeID: nodeID, ResetIndex: resetIndex,
		ExistingSuccessor: existing,
	})
	if !decision.Admitted {
		switch decision.Code {
		case "AGENT_DRIVER_TOOLS_PENDING":
			return errAgentDriverTools
		case "EFFECT_UNKNOWN":
			return errAgentDriverUnknown
		case "AGENT_RUNTIME_RESET_REQUIRES_GENERATION":
			return errAgentDriverReset
		default:
			return errAgentDriverUnknown
		}
	}
	if decision.RetainedOperationID != operationID || decision.SuccessorOperationID == "" || decision.SuccessorOperationID == operationID {
		return errAgentDriverReset
	}
	if control == nil || claim == nil || claim.Fence == nil || claim.Owner == "" || claim.ClaimToken == "" {
		return errAgentDriverReset
	}
	bound, err := control.BindAgentNodeSuccessor(store.AgentNodeSuccessorBindRequest{
		OperationID: driverOperation("bind-successor:", actionID, ":", epoch, ":", nodeID), Actor: agentDriverActor,
		ActionID: actionID, Epoch: epoch, RunID: claim.RunId, NodeID: nodeID, Owner: claim.Owner, ClaimToken: claim.ClaimToken,
		ResetIndex: resetIndex, Effect: effectState, ResetAdmitted: resetAdmitted, ResetFenceMatches: resetAdmitted,
	})
	if err != nil {
		var refused *store.AgentSuccessorError
		if errors.As(err, &refused) {
			return successorAdmissionError(refused.Code)
		}
		return err
	}
	if bound.OperationID != operationID || bound.SuccessorOperationID != decision.SuccessorOperationID || bound.Replaced {
		return errAgentDriverReset
	}
	return errAgentDriverHeld
}

// Run the stored generation only after the authority decision admits the
// epoch that claim or takeover already wrote. This function does not install
// an epoch.
func (driver *agentRunDriver) successorExecute(ctx context.Context, client agentDriverWorker, nodes *agentNodeService, claim *agentv1.AgentRunExecutionClaim, grant []byte, id, operationID, effectState string) (*agentv1.AgentNodeEffectMetadata, error) {
	if nodes == nil || nodes.run == nil || nodes.run.control == nil || claim == nil || claim.Fence == nil || client == nil {
		return nil, errAgentDriverUnknown
	}
	control := nodes.run.control
	record, intent, found, err := control.AgentNodeDispatchSnapshot(claim.Fence.ActionId, id)
	if err != nil || !found || intent.OperationID != operationID || intent.OperationID == "" {
		return nil, errAgentDriverUnknown
	}
	index, resetAdmitted, err := resetSharingFence(control, claim.RunId, id, intent.ActionID, intent.ExecutionEpoch)
	if err != nil {
		return nil, err
	}
	decision := agent.SuccessorExecution(agent.SuccessorExecutionInput{
		IdentityValid: true, Lease: "live", Epoch: "match", Effect: effectState,
		ResetAdmitted: resetAdmitted, ResetFenceMatches: resetAdmitted,
		OperationID: intent.OperationID, ActionID: intent.ActionID, ExecutionEpoch: intent.ExecutionEpoch,
		NodeID: intent.NodeID, ResetIndex: index, StoredSuccessor: intent.SuccessorOperationID,
		StoredOperationID: intent.OperationID, AuthorityIssued: record.ExecutionEpoch > intent.ExecutionEpoch,
		WorkerPromoted: false, AuthorityEpoch: record.ExecutionEpoch, PresentedEpoch: claim.Fence.ExecutionEpoch,
	})
	if !decision.Execute || decision.Promoted {
		switch decision.Code {
		case "EFFECT_UNKNOWN":
			return nil, errAgentDriverUnknown
		case "STALE_EXECUTION_EPOCH":
			return nil, errAgentDriverStale
		case "FENCE_MISMATCH":
			return nil, errAgentDriverFence
		case "AGENT_RUNTIME_SUCCESSOR_HELD":
			return nil, errAgentDriverHeld
		default:
			return nil, successorAdmissionError(decision.Code)
		}
	}
	if claim.ReconciliationRequired {
		admitted, err := control.AdmitSuccessorDispatch(store.AgentSuccessorEpochRequest{
			OperationID: driverOperation("admit-successor:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch, ":", id),
			Actor:       agentDriverActor, ActionID: claim.Fence.ActionId, Epoch: claim.Fence.ExecutionEpoch,
			RunID: claim.RunId, NodeID: id, Owner: claim.Owner, ClaimToken: claim.ClaimToken,
			Effect: effectState, ResetAdmitted: resetAdmitted, ResetFenceMatches: resetAdmitted, ResetIndex: index,
		})
		if err != nil {
			var refused *store.AgentSuccessorError
			if errors.As(err, &refused) {
				return nil, successorAdmissionError(refused.Code)
			}
			return nil, err
		}
		rebuilt, err := executionClaimProtocol(admitted, control.Writer().LeaseUntil)
		if err != nil {
			return nil, err
		}
		claim = rebuilt
	}
	renewed, nextGrant, err := driver.grant(ctx, claim)
	if err != nil {
		return nil, err
	}
	claim, grant = renewed, nextGrant
	if claim == nil || claim.Fence == nil || decision.OperationID == "" || decision.OperationID == decision.RetainedOperationID ||
		decision.ExecutionEpoch != claim.Fence.ExecutionEpoch || decision.Promoted {
		return nil, errAgentDriverHeld
	}
	operation := decision.OperationID
	var effect *agentv1.AgentNodeEffectMetadata
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
	if err != nil {
		return nil, err
	}
	if effect == nil {
		return nil, errAgentDriverUnknown
	}
	if code, fresh := agent.DispatchAllowed("live", "match", effect.State); !fresh {
		if code == "AGENT_DRIVER_TOOLS_PENDING" || effect.State == "AWAITING_TOOLS" {
			decision, observeErr := driver.retainedDriverToolResults(claim, effect)
			if observeErr != nil {
				return nil, observeErr
			}
			if !decision.Returned || len(decision.Events) == 0 {
				return nil, errAgentDriverTools
			}
			return effect, nil
		}
		// A published answer ends the tool wait. Recording its receipt stays
		// outside this branch.
		if effect.State == "ANSWER_PUBLISHED" {
			return effect, nil
		}
		return nil, errAgentDriverUnknown
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
		readyState := ""
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
				readyState = states[entry.ID].State
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
		effect, err := driver.node(ctx, client, nodes, claim, grant, ready, readyState)
		if err != nil {
			return err
		}
		// A tool wait has no receipt. A published answer settles only from the
		// receipt Rust already signed. A missing receipt stays on this run.
		if effect.State == "AWAITING_TOOLS" {
			return nil
		}
		if effect.State == "ANSWER_PUBLISHED" {
			receipt := effect.OutcomeReceipt
			if receipt == nil || receipt.Evidence == nil || receipt.Evidence.TaskOutput == nil {
				return nil
			}
			_, err = nodes.RecordOutcome(ctx, &agentv1.RecordAgentNodeOutcomeRequest{SchemaVersion: 1,
				OperationId: driverOperation("outcome:", claim.Fence.ActionId, ":", claim.Fence.ExecutionEpoch, ":", ready), Actor: agentDriverActor,
				Fence: claim.Fence, RunId: run.RunID, NodeId: ready, OutcomeReceipt: effect.OutcomeReceipt})
			if err != nil {
				return err
			}
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

// Cancelled recovery observes only retained dispatches. It cannot issue a grant,
// execute a node, project output or schedule dependencies/synthesis.
func (driver *agentRunDriver) recoverCancellation(ctx context.Context, run agent.RunMetadata) error {
	if !run.Cancelled || run.Status != "cancelled" {
		return errAgentDriverUnknown
	}
	if err := driver.service.executionProcess(); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil {
		return err
	}
	for _, record := range records {
		binding, bindErr := store.AgentRunExecutionBindingFromRecord(record)
		if bindErr != nil || binding.RunID != run.RunID {
			continue
		}
		if record.State == "SUCCEEDED" || record.State == "CANCELLED_CONFIRMED" {
			continue
		}
		if binding.Request != run.Request {
			return errAgentDriverUnknown
		}
		lease, found, err := driver.service.control.GetActionLease(record.ID)
		if err != nil || !found {
			return errAgentDriverUnknown
		}
		if lease.Owner != driver.owner || lease.WriterFencingToken != driver.service.control.Writer().FencingToken {
			_, claimErr := driver.service.control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: record.ID, Owner: driver.owner, LeaseSeconds: agentDriverLease})
			if claimErr != nil {
				return claimErr
			}
			lease, found, err = driver.service.control.GetActionLease(record.ID)
			if err != nil || !found {
				return errAgentDriverUnknown
			}
		}
		var session *agentExecutionSignerSession
		defer func() {
			if session != nil && session.close != nil {
				_ = session.close()
			}
		}()
		nodes := &agentNodeService{run: driver.service}
		fence := &commonv1.ActionFence{ActionId: record.ID, ExecutionEpoch: uint64(lease.Epoch)}
		ids := make([]string, 0, len(run.Plan)+1)
		if binding.Phase == "plan" {
			ids = append(ids, "leader")
		} else if binding.Phase == "synthesis" {
			ids = append(ids, "synthesizer")
		} else {
			for _, node := range run.Plan {
				ids = append(ids, node.ID)
			}
		}
		for _, id := range ids {
			dispatch, err := nodes.GetDispatch(ctx, &agentv1.GetAgentNodeDispatchRequest{Fence: fence, RunId: run.RunID, NodeId: id})
			if err != nil {
				return err
			}
			if !dispatch.Found || dispatch.OutcomeReceipt != nil {
				continue
			}
			if dispatch.Intent == nil {
				return errAgentDriverUnknown
			}
			if session == nil {
				factory := driver.service.executionSigner
				if factory == nil {
					factory = configuredAgentExecutionSigner
				}
				session, err = factory(driver.service.control)
				if err != nil {
					return err
				}
				if session == nil || session.client == nil {
					return errAgentDriverCustody
				}
			}
			client, ok := session.client.(agentDriverWorker)
			if !ok {
				return errAgentDriverCustody
			}
			effect, err := client.QueryAgentNodeEffect(ctx, &agentv1.QueryAgentNodeEffectRequest{OperationId: dispatch.Intent.OperationId, Fence: dispatch.Intent.Fence, RunId: run.RunID, NodeId: id})
			if projectErr := driver.projectJournalObservation(run.RunID, id, effect, err); projectErr != nil {
				return projectErr
			}
			if err != nil {
				return err
			}
			if effect == nil || effect.State != "CONFIRMED" || effect.OutcomeReceipt == nil {
				return errAgentDriverUnknown
			}
			_, err = nodes.RecordOutcome(ctx, &agentv1.RecordAgentNodeOutcomeRequest{SchemaVersion: 1, OperationId: driverOperation("cancel-outcome:", record.ID, ":", lease.Epoch, ":", id), Actor: agentDriverActor, Fence: fence, RunId: run.RunID, NodeId: id, OutcomeReceipt: effect.OutcomeReceipt})
			if err != nil {
				return err
			}
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		_, err = driver.service.control.SettleAgentCancellation(store.SettleAgentCancellationRequest{OperationID: driverOperation("cancel-settle:", record.ID, ":", lease.Epoch), Actor: agentDriverActor, ActionID: record.ID, Epoch: uint64(lease.Epoch), RunID: run.RunID, Owner: driver.owner, ClaimToken: lease.ClaimToken, ExpectedIndex: run.NextIndex})
		if errors.Is(err, store.ErrActionLeaseExpired) {
			_, claimErr := driver.service.control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: record.ID, Owner: driver.owner, LeaseSeconds: agentDriverLease})
			if claimErr != nil {
				return claimErr
			}
			return errAgentDriverUnknown
		}
		if err != nil {
			return err
		}
	}
	return nil
}
