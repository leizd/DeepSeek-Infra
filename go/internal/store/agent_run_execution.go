package store

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"unicode/utf8"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

var ErrAgentRunExecutionRequired = errors.New("AGENT_RUN_EXECUTION_REQUIRED")

// The metadata fence selects the live Go-owned run. The returned action lease
// has a different action ID and epoch; metadata never grants provider effects.
type AgentRunExecutionRequest struct {
	OperationID   string                        `json:"operationId"`
	Actor         string                        `json:"actor"`
	RunID         string                        `json:"runId"`
	Phase         string                        `json:"phase"`
	ExpectedIndex int64                         `json:"expectedIndex"`
	Fence         *internalprotocol.ActionFence `json:"fence"`
	Owner         string                        `json:"owner"`
	LeaseSeconds  int64                         `json:"leaseSeconds"`
}

type AgentRunExecutionResult struct {
	Mutation         OperatorMutationResult `json:"mutation"`
	Admission        AdmissionResult        `json:"admission"`
	WriterLeaseUntil int64                  `json:"writerLeaseUntil"`
}

type AgentRunExecutionBinding struct {
	SchemaVersion uint32            `json:"schemaVersion"`
	RunID         string            `json:"runId"`
	Phase         string            `json:"phase"`
	MetadataIndex int64             `json:"metadataIndex"`
	MetadataEpoch uint64            `json:"metadataEpoch"`
	Request       agent.ArtifactRef `json:"requestRef"`
	PlanDigest    string            `json:"planDigest"`
}

type agentExecutionIntent = AgentRunExecutionBinding

func nativeAgentExecution(payload json.RawMessage) bool {
	var fields map[string]json.RawMessage
	if json.Unmarshal(payload, &fields) != nil {
		return false
	}
	_, marked := fields["nativeAgentExecution"]
	return marked
}

func executionActionID(intent agentExecutionIntent) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("agent-run-execution-v1\n%s\n%s\n%d\n", intent.RunID, intent.Phase, intent.MetadataIndex)))
	return "agent-exec-" + hex.EncodeToString(sum[:])
}

func executionPlanDigest(run agent.RunMetadata) string {
	bytes, _ := json.Marshal(run.Plan)
	sum := sha256.Sum256(bytes)
	return hex.EncodeToString(sum[:])
}

func decodeAgentExecution(record Record) (agentExecutionIntent, error) {
	var fields map[string]json.RawMessage
	err := json.Unmarshal(record.Payload, &fields)
	var intent agentExecutionIntent
	decoder := json.NewDecoder(bytes.NewReader(fields["nativeAgentExecution"]))
	decoder.DisallowUnknownFields()
	decodeErr := decoder.Decode(&intent)
	digest, digestErr := hex.DecodeString(intent.PlanDigest)
	if err != nil || decodeErr != nil || record.Domain != "action" || intent.SchemaVersion != 1 || !ValidRecordID(intent.RunID) ||
		(intent.Phase != "plan" && intent.Phase != "tasks") || intent.MetadataIndex < 1 || intent.MetadataIndex >= math.MaxInt64-1 ||
		intent.MetadataEpoch == 0 || intent.MetadataEpoch > math.MaxInt64 || !validAgentArtifact(intent.Request) ||
		digestErr != nil || len(digest) != 32 || intent.PlanDigest != strings.ToLower(intent.PlanDigest) ||
		record.ID != executionActionID(intent) {
		return agentExecutionIntent{}, ErrAgentRunExecutionRequired
	}
	return intent, nil
}

// The complete body stays in Rust custody. Only this immutable scope and
// artifact identity cross the authenticated control RPC.
func AgentRunExecutionBindingFromRecord(record Record) (AgentRunExecutionBinding, error) {
	return decodeAgentExecution(record)
}

func sameAgentExecutionIntent(before, after Record) bool {
	first, err := decodeAgentExecution(before)
	second, otherErr := decodeAgentExecution(after)
	return err == nil && otherErr == nil && first == second
}

func readAgentExecutionActionTx(tx *sql.Tx, id string) (Record, bool, error) {
	record, found, err := readControlRecord(tx.QueryRow(
		`SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM action_journal WHERE id=?`, id), "action")
	if err == nil && found {
		err = validateControlHistory(tx, record)
	}
	return record, found, err
}

func executionPhaseReady(run agent.RunMetadata, phase string) bool {
	return phase == "plan" && (run.Status == "created" || run.Status == "planning") ||
		phase == "tasks" && run.Status == "running" && len(run.Plan) > 0
}

// The caller has decoded the immutable binding. All three lease paths read
// authority and the current run once inside their own write transaction.
func (store *Control) agentExecutionRunTx(tx *sql.Tx, intent agentExecutionIntent) (agent.RunMetadata, error) {
	if !store.authorizeCutover {
		return agent.RunMetadata{}, ErrCutoverNotAuthorized
	}
	for _, domain := range []string{"agent_run", "action"} {
		cutover, err := readCutoverTx(tx, domain)
		if err != nil {
			return agent.RunMetadata{}, err
		}
		if !IsDomainGoAuthoritative(cutover.State) {
			return agent.RunMetadata{}, ErrCutoverNotAuthorized
		}
		if domain == "agent_run" && uint64(cutover.Epoch) != intent.MetadataEpoch {
			return agent.RunMetadata{}, internalprotocol.ErrFenceMismatch
		}
	}
	runRecord, found, err := readAgentRecordTx(tx, intent.RunID)
	if err != nil {
		return agent.RunMetadata{}, err
	}
	if !found {
		return agent.RunMetadata{}, ErrActionNotClaimable
	}
	run, err := decodeAgentMetadata(runRecord)
	if err != nil {
		return agent.RunMetadata{}, err
	}
	if run.Request != intent.Request {
		return agent.RunMetadata{}, ErrAgentRunExecutionRequired
	}
	return run, nil
}

// Generic admission retains the mandatory run reservation. Takeover preserves
// the old scope and enters RECONCILING; only a fresh claim checks this cursor.
func (store *Control) agentExecutionAdmissionTx(tx *sql.Tx, record Record, takeover bool) ([]string, error) {
	if !nativeAgentExecution(record.Payload) {
		return nil, nil
	}
	intent, err := decodeAgentExecution(record)
	if err != nil {
		return nil, err
	}
	run, err := store.agentExecutionRunTx(tx, intent)
	if err != nil {
		return nil, err
	}
	if !takeover && (run.NextIndex != intent.MetadataIndex || executionPlanDigest(run) != intent.PlanDigest || !executionPhaseReady(run, intent.Phase)) {
		return nil, ErrActionNotClaimable
	}
	return []string{"agent-run:" + intent.RunID}, nil
}

func (store *Control) agentExecutionRenewalTx(tx *sql.Tx, record Record) error {
	if !nativeAgentExecution(record.Payload) {
		return nil
	}
	intent, err := decodeAgentExecution(record)
	if err != nil {
		return err
	}
	run, err := store.agentExecutionRunTx(tx, intent)
	if err != nil {
		return err
	}
	// Reconciliation and verification retain their original reservations. Their
	// lease permits observation and settlement, never another fresh dispatch.
	if record.State != "CLAIMED" && record.State != "EXECUTING" {
		return nil
	}
	if !executionPhaseReady(run, intent.Phase) || executionPlanDigest(run) != intent.PlanDigest {
		return ErrActionNotClaimable
	}
	return nil
}

func normalizeExecutionRequest(input AgentRunExecutionRequest) (AgentRunExecutionRequest, error) {
	request := input
	if input.Fence != nil {
		// Snapshot the admission fields without JSON's lossy UTF-8 repair. Raw
		// identities must be validated before they enter the replay digest.
		request.Fence = &internalprotocol.ActionFence{ActionId: input.Fence.ActionId, ExecutionEpoch: input.Fence.ExecutionEpoch}
	}
	request.Owner = strings.TrimSpace(request.Owner)
	if request.LeaseSeconds == 0 {
		request.LeaseSeconds = 60
	}
	if !ValidRecordID(request.RunID) || !utf8.ValidString(request.RunID) || len(request.RunID) > 256 || request.ExpectedIndex < 1 || request.ExpectedIndex >= math.MaxInt64-1 ||
		(request.Phase != "plan" && request.Phase != "tasks") || request.LeaseSeconds < 1 || request.LeaseSeconds > 300 {
		return request, ErrAgentRunExecutionRequired
	}
	for _, text := range []string{request.OperationID, request.Actor, request.Owner} {
		if text == "" || len(text) > 256 || strings.ContainsRune(text, 0) || !utf8.ValidString(text) {
			return request, ErrAgentRunExecutionRequired
		}
	}
	return request, nil
}

// This is the production admission primitive, not a new metadata permission.
// The two domain authorities, immutable request/plan binding, pending action,
// lease, resource reservation and replay receipt are admitted and committed in
// one SQLite transaction. Rust still needs a signed local epoch and a journaled
// worker operation before this lease can result in any provider effect.
func (store *Control) ClaimAgentRunExecution(input AgentRunExecutionRequest) (AgentRunExecutionResult, error) {
	request, err := normalizeExecutionRequest(input)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	encoded, err := json.Marshal(request)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(request.OperationID, request.RunID, request.Actor, "agent_run")
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	defer tx.Rollback()
	if request.Fence == nil || request.Fence.ActionId != request.RunID {
		return AgentRunExecutionResult{}, internalprotocol.ErrFenceMismatch
	}
	if err := internalprotocol.AdmitCommand(request.Fence, uint64(context.cutover.Epoch)); err != nil {
		return AgentRunExecutionResult{}, err
	}
	actionAuthority, err := readCutoverTx(tx, "action")
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	if !IsDomainGoAuthoritative(actionAuthority.State) {
		return AgentRunExecutionResult{}, ErrCutoverNotAuthorized
	}
	replay, applied, err := replayOperatorMutationTx(tx, request.OperationID, requestDigest)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	if applied {
		result, err := store.replayAgentExecutionTx(tx, request, replay, context.now)
		if err != nil {
			return AgentRunExecutionResult{}, err
		}
		return store.commitAgentExecutionTx(tx, context, result)
	}
	runRecord, found, err := readAgentRecordTx(tx, request.RunID)
	if err != nil || !found {
		if err == nil {
			err = ErrActionNotClaimable
		}
		return AgentRunExecutionResult{}, err
	}
	run, err := decodeAgentMetadata(runRecord)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	if run.NextIndex != request.ExpectedIndex {
		return AgentRunExecutionResult{}, ErrRevisionConflict
	}
	if !executionPhaseReady(run, request.Phase) {
		return AgentRunExecutionResult{}, ErrActionNotClaimable
	}
	intent := agentExecutionIntent{SchemaVersion: 1, RunID: run.RunID, Phase: request.Phase, MetadataIndex: run.NextIndex,
		MetadataEpoch: uint64(context.cutover.Epoch), Request: run.Request, PlanDigest: executionPlanDigest(run)}
	payload, _ := json.Marshal(struct {
		Intent agentExecutionIntent `json:"nativeAgentExecution"`
	}{intent})
	actionID := executionActionID(intent)
	existing, exists, err := readAgentExecutionActionTx(tx, actionID)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	if exists {
		current, err := decodeAgentExecution(existing)
		if err != nil || current != intent {
			return AgentRunExecutionResult{}, ErrAgentRunExecutionRequired
		}
	} else {
		write, err := prepareControlRecordWrite(Record{Domain: "action", ID: actionID, Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: payload}, nil)
		if err != nil {
			return AgentRunExecutionResult{}, err
		}
		write.agentExecution = true
		if err := store.putControlRecordTx(tx, write, context.now); err != nil {
			return AgentRunExecutionResult{}, err
		}
	}
	admission, err := store.admitAndClaimActionTx(tx, AdmissionRequest{ActionID: actionID, Owner: request.Owner, LeaseSeconds: request.LeaseSeconds}, context.now, context.leaseUntil)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(runRecord.Payload)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	mutation := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: request.OperationID, Domain: "agent_run", RecordID: request.RunID,
		Revision: run.NextIndex, State: run.Status, ActionID: actionID, ExecutionEpoch: admission.Lease.Epoch, CutoverRevision: context.cutover.Revision,
		FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest, RequestDigest: requestDigest, Actor: request.Actor}
	if err := insertOperatorMutationTx(tx, mutation, store.token, context.now); err != nil {
		return AgentRunExecutionResult{}, err
	}
	return store.commitAgentExecutionTx(tx, context, AgentRunExecutionResult{Mutation: mutation, Admission: admission})
}

func (store *Control) commitAgentExecutionTx(tx *sql.Tx, context operatorMutationContext, result AgentRunExecutionResult) (AgentRunExecutionResult, error) {
	commitNow := store.now()
	if commitNow < context.now || commitNow >= context.leaseUntil {
		return AgentRunExecutionResult{}, ErrWriterFenceHeld
	}
	if commitNow >= result.Admission.Lease.LeaseUntil {
		return AgentRunExecutionResult{}, ErrActionLeaseExpired
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentRunExecutionResult{}, err
	}
	result.WriterLeaseUntil = context.leaseUntil
	return result, nil
}

func (store *Control) replayAgentExecutionTx(tx *sql.Tx, request AgentRunExecutionRequest, mutation OperatorMutationResult, now int64) (AgentRunExecutionResult, error) {
	if mutation.Domain != "agent_run" || mutation.RecordID != request.RunID {
		return AgentRunExecutionResult{}, ErrCorruptRecord
	}
	record, found, err := readAgentExecutionActionTx(tx, mutation.ActionID)
	if err != nil || !found {
		return AgentRunExecutionResult{}, ErrCorruptRecord
	}
	intent, err := decodeAgentExecution(record)
	if err != nil || intent.RunID != request.RunID || intent.MetadataIndex != request.ExpectedIndex || intent.Phase != request.Phase {
		return AgentRunExecutionResult{}, ErrCorruptRecord
	}
	run, err := store.agentExecutionRunTx(tx, intent)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	if !executionPhaseReady(run, intent.Phase) || executionPlanDigest(run) != intent.PlanDigest {
		return AgentRunExecutionResult{}, ErrActionNotClaimable
	}
	lease, err := readActiveActionLeaseTx(tx, record.ID)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	if lease.Owner != request.Owner || lease.WriterFencingToken != store.token || lease.Epoch != mutation.ExecutionEpoch || record.ExecutionEpoch != lease.Epoch {
		return AgentRunExecutionResult{}, ErrActionLeaseStale
	}
	if now < lease.UpdatedAt || now >= lease.LeaseUntil {
		return AgentRunExecutionResult{}, ErrActionLeaseExpired
	}
	manifest, _, err := validateActionLeaseResourcesTx(tx, lease)
	if err != nil {
		return AgentRunExecutionResult{}, err
	}
	var keys []string
	if err := json.Unmarshal([]byte(manifest), &keys); err != nil {
		return AgentRunExecutionResult{}, ErrActionLeaseStale
	}
	return AgentRunExecutionResult{Mutation: mutation, Admission: AdmissionResult{Lease: lease, Record: record, ResourceKeys: keys}}, nil
}
