package store

import (
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

var ErrAgentRunMetadataInvalid = errors.New("AGENT_RUN_METADATA_INVALID")
var ErrAgentRunEventRequired = errors.New("AGENT_RUN_EVENT_REQUIRED")

func nativeAgentMetadata(payload json.RawMessage) bool {
	var fields map[string]json.RawMessage
	if json.Unmarshal(payload, &fields) != nil {
		return false
	}
	_, marked := fields["agentMetadataSchemaVersion"]
	return marked
}

type AgentRunEventMutation struct {
	OperationID   string                        `json:"operationId"`
	Actor         string                        `json:"actor"`
	RunID         string                        `json:"runId"`
	ExpectedIndex int64                         `json:"expectedIndex"`
	Fence         *internalprotocol.ActionFence `json:"fence"`
	Create        *agent.RunMetadata            `json:"create,omitempty"`
	Event         agent.RunEvent                `json:"event"`
}

type AgentRunEventResult struct {
	Mutation OperatorMutationResult `json:"mutation"`
	Event    agent.RunEvent         `json:"event"`
	Run      agent.RunMetadata      `json:"run"`
}

func validAgentArtifact(ref agent.ArtifactRef) bool {
	decoded, err := hex.DecodeString(ref.SHA256)
	return err == nil && len(decoded) == 32 && ref.SHA256 == strings.ToLower(ref.SHA256) && ref.Length > 0 && ref.Length <= 128<<20
}

func decodeAgentMetadata(record Record) (agent.RunMetadata, error) {
	var run agent.RunMetadata
	if err := json.Unmarshal(record.Payload, &run); err != nil {
		return run, ErrAgentRunMetadataInvalid
	}
	if run.SchemaVersion != 1 || run.RunID != record.ID || run.Status != record.State ||
		run.NextIndex != record.Revision || run.LastEvent.Index != record.Revision-1 ||
		run.LastEvent.RunID != record.ID || run.LastEvent.ActionID != record.ID ||
		run.LastEvent.ExecutionEpoch != record.ExecutionEpoch || !validAgentArtifact(run.Request) ||
		!validAgentArtifact(run.LastEvent.Body) || run.BaseNodes == nil {
		return run, ErrAgentRunMetadataInvalid
	}
	return run, nil
}

func readAgentRecordTx(tx *sql.Tx, id string) (Record, bool, error) {
	record, found, err := readControlRecord(tx.QueryRow(
		`SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM agent_runs WHERE id=?`, id), "agent_run")
	if err == nil && found {
		err = validateControlHistory(tx, record)
	}
	return record, found, err
}

// AppendAgentRunEvent uses the real operator authority transaction. Cursor,
// projection, immutable control history and operation receipt commit together.
// Neither artifact references nor projected node states grant execution rights.
func (store *Control) AppendAgentRunEvent(input AgentRunEventMutation) (AgentRunEventResult, error) {
	if !ValidRecordID(input.RunID) || input.ExpectedIndex < 0 || input.ExpectedIndex >= math.MaxInt64-1 ||
		input.Event.Type == "" || len(input.Event.Type) > 64 || !validAgentArtifact(input.Event.Body) {
		return AgentRunEventResult{}, ErrAgentRunMetadataInvalid
	}
	// Detach all caller-owned slices and pointers before normalization or stamping.
	encoded, err := json.Marshal(input)
	if err != nil {
		return AgentRunEventResult{}, err
	}
	var mutation AgentRunEventMutation
	if err := json.Unmarshal(encoded, &mutation); err != nil {
		return AgentRunEventResult{}, err
	}
	if input.Event.Type == "agent_plan" {
		mutation.Event.Plan = agent.NormalizeRunPlan(input.Event.Plan)
	}
	mutation.Event.RunID, mutation.Event.Index, mutation.Event.CreatedAt = "", 0, ""
	mutation.Event.ActionID, mutation.Event.ExecutionEpoch = "", 0
	encoded, err = json.Marshal(mutation)
	if err != nil {
		return AgentRunEventResult{}, err
	}
	requestDigest, err := operatorPayloadDigest(encoded)
	if err != nil {
		return AgentRunEventResult{}, err
	}

	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(mutation.OperationID, mutation.RunID, mutation.Actor, "agent_run")
	if err != nil {
		return AgentRunEventResult{}, err
	}
	defer tx.Rollback()
	if mutation.Fence == nil || mutation.Fence.ActionId != mutation.RunID {
		return AgentRunEventResult{}, internalprotocol.ErrFenceMismatch
	}
	if err := internalprotocol.AdmitCommand(mutation.Fence, uint64(context.cutover.Epoch)); err != nil {
		return AgentRunEventResult{}, err
	}
	replay, applied, err := replayOperatorMutationTx(tx, mutation.OperationID, requestDigest)
	if err != nil {
		return AgentRunEventResult{}, err
	}
	record, found, err := readAgentRecordTx(tx, mutation.RunID)
	if err != nil {
		return AgentRunEventResult{}, err
	}
	var run agent.RunMetadata
	if found {
		run, err = decodeAgentMetadata(record)
		if err != nil {
			return AgentRunEventResult{}, err
		}
	}
	if applied {
		if !found || replay.Domain != "agent_run" || replay.RecordID != mutation.RunID {
			return AgentRunEventResult{}, ErrCorruptRecord
		}
		var payload string
		if err := tx.QueryRow(`SELECT payload_json FROM control_events WHERE domain='agent_run' AND record_id=? AND revision=?`, mutation.RunID, replay.Revision).Scan(&payload); err != nil {
			return AgentRunEventResult{}, err
		}
		var original agent.RunMetadata
		if err := json.Unmarshal([]byte(payload), &original); err != nil {
			return AgentRunEventResult{}, ErrCorruptRecord
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return AgentRunEventResult{}, err
		}
		return AgentRunEventResult{Mutation: replay, Event: original.LastEvent, Run: run}, nil
	}
	if run.NextIndex != mutation.ExpectedIndex {
		return AgentRunEventResult{}, ErrRevisionConflict
	}
	if !found {
		if mutation.Create == nil || mutation.Event.Type != "run_status" || mutation.Event.Status != "created" || !validAgentArtifact(mutation.Create.Request) {
			return AgentRunEventResult{}, ErrAgentRunMetadataInvalid
		}
		create := mutation.Create
		run = agent.RunMetadata{SchemaVersion: 1, RunID: mutation.RunID, Status: "created", Preset: create.Preset, ConfirmPlan: create.ConfirmPlan,
			ConversationID: create.ConversationID, MessageID: create.MessageID, Request: create.Request,
			Plan: []agent.RunPlanNode{}, BaseNodes: map[string]agent.RunNode{}, FinalAfter: -1}
	} else if mutation.Create != nil {
		return AgentRunEventResult{}, ErrAgentRunMetadataInvalid
	}
	event := mutation.Event
	event.RunID, event.Index, event.CreatedAt = mutation.RunID, mutation.ExpectedIndex, time.Unix(context.now, 0).UTC().Format(time.RFC3339)
	event.ActionID, event.ExecutionEpoch = mutation.RunID, uint64(context.cutover.Epoch)
	run.ApplyRunEvent(event)
	payload, err := json.Marshal(run)
	if err != nil {
		return AgentRunEventResult{}, err
	}
	record = Record{Domain: "agent_run", ID: run.RunID, State: run.Status, Revision: run.NextIndex, ExecutionEpoch: event.ExecutionEpoch, Payload: payload}
	write, err := prepareControlRecordWrite(record, nil)
	if err != nil {
		return AgentRunEventResult{}, err
	}
	write.agentEvent = true
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return AgentRunEventResult{}, err
	}
	digest, err := operatorPayloadDigest(write.record.Payload)
	if err != nil {
		return AgentRunEventResult{}, err
	}
	result := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: mutation.OperationID, Domain: record.Domain, RecordID: record.ID,
		Revision: record.Revision, State: record.State, ActionID: mutation.RunID, ExecutionEpoch: event.ExecutionEpoch,
		CutoverRevision: context.cutover.Revision, FencingToken: context.cutover.FencingToken, PayloadDigest: digest, RequestDigest: requestDigest, Actor: mutation.Actor}
	if err := insertOperatorMutationTx(tx, result, store.token, context.now); err != nil {
		return AgentRunEventResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return AgentRunEventResult{}, err
	}
	return AgentRunEventResult{Mutation: result, Event: event, Run: run}, nil
}

func (store *Control) AgentRunMetadata(id string) (agent.RunMetadata, bool, error) {
	record, found, err := store.Get("agent_run", id)
	if err != nil || !found {
		return agent.RunMetadata{}, found, err
	}
	run, err := decodeAgentMetadata(record)
	return run, true, err
}

// AgentRunEventsAfter pages immutable headers. Rust resolves event bodies from
// its own custody and overlays these authoritative identity/cursor/fence fields.
func (store *Control) AgentRunEventsAfter(id string, after int64, limit int) ([]agent.RunEvent, error) {
	if !ValidRecordID(id) || after < -1 || limit < 1 || limit > 1024 {
		return nil, ErrAgentRunMetadataInvalid
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return nil, ErrWriterFenceHeld
	}
	tx, err := store.db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return nil, err
	}
	record, found, err := readAgentRecordTx(tx, id)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, ErrEmptyRecordID
	}
	if _, err := decodeAgentMetadata(record); err != nil {
		return nil, err
	}
	events := []agent.RunEvent{}
	if after >= record.Revision-1 {
		return events, nil
	}
	rows, err := tx.Query(`SELECT payload_json FROM control_events WHERE domain='agent_run' AND record_id=? AND revision>? ORDER BY revision LIMIT ?`, id, after+1, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var payload string
		if err := rows.Scan(&payload); err != nil {
			return nil, err
		}
		var snapshot agent.RunMetadata
		if err := json.Unmarshal([]byte(payload), &snapshot); err != nil {
			return nil, ErrCorruptRecord
		}
		events = append(events, snapshot.LastEvent)
	}
	return events, rows.Err()
}
