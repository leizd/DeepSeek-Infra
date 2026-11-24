package agent

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
)

// SuccessorInput is the fenced decision for one admitted agent_reset.
// Epoch is the relation to the live fence, not a number the worker may promote.
type SuccessorInput struct {
	IdentityValid     bool
	Lease             string
	Epoch             string
	Effect            string
	ResetAdmitted     bool
	ResetFenceMatches bool
	OperationID       string
	ActionID          string
	ExecutionEpoch    uint64
	NodeID            string
	ResetIndex        int64
	ExistingSuccessor string
}

// SuccessorDecision keeps the original operation id. SuccessorOperationID is
// empty unless this reset may name a later generation.
type SuccessorDecision struct {
	Admitted             bool
	Code                 string
	Message              string
	RetainedOperationID  string
	SuccessorOperationID string
	Dispatch             string
	Replayed             bool
}

func successorRefuse(retained, code, message string) SuccessorDecision {
	return SuccessorDecision{Code: code, Message: message, RetainedOperationID: retained, Dispatch: "none"}
}

func canonicalToken(value string, limit int) bool {
	return value != "" && len(value) <= limit && !strings.ContainsAny(value, "\n\r")
}

// SuccessorOperationID is the SHA-256 of the shared material. Rust and the
// Python oracle hash the same line.
func SuccessorOperationID(operationID, actionID string, epoch uint64, nodeID string, resetIndex int64) string {
	material := fmt.Sprintf("agent-node-successor-v1\n%s\n%s\n%d\n%s\n%d", operationID, actionID, epoch, nodeID, resetIndex)
	sum := sha256.Sum256([]byte(material))
	return "agent-successor-" + hex.EncodeToString(sum[:])
}

// SuccessorGeneration mints a new operation id only when the prior effect is
// settled and the reset event is on this fence. EFFECT_UNKNOWN keeps the old
// id and does not receive a successor.
func SuccessorGeneration(input SuccessorInput) SuccessorDecision {
	retained := input.OperationID
	if !input.IdentityValid {
		return successorRefuse(retained, "INVALID_IDENTITY", "Agent runtime identity is invalid")
	}
	if input.Lease != "live" {
		return successorRefuse(retained, "LEASE_NOT_HELD", "Agent runtime lease is not live")
	}
	if input.Epoch == "stale" {
		return successorRefuse(retained, "STALE_EXECUTION_EPOCH", "Agent runtime epoch is stale")
	}
	if input.Epoch != "match" {
		return successorRefuse(retained, "FENCE_MISMATCH", "Agent runtime fence does not match")
	}
	if input.Effect == "AWAITING_TOOLS" {
		return successorRefuse(retained, "AGENT_DRIVER_TOOLS_PENDING", "Tool continuation stays on the original effect")
	}
	if input.Effect != "CONFIRMED" && input.Effect != "SETTLED_FAILURE" {
		return successorRefuse(retained, "EFFECT_UNKNOWN", "Agent runtime effect is unknown and was not retried")
	}
	if !input.ResetAdmitted || !input.ResetFenceMatches {
		return successorRefuse(retained, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "Agent reset requires a settled effect on the same fence")
	}
	if !canonicalToken(input.OperationID, 256) || !canonicalToken(input.ActionID, 256) || !canonicalToken(input.NodeID, 64) || input.ExecutionEpoch == 0 || input.ResetIndex < 0 {
		return successorRefuse(retained, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "Agent reset requires a settled effect on the same fence")
	}
	successor := SuccessorOperationID(input.OperationID, input.ActionID, input.ExecutionEpoch, input.NodeID, input.ResetIndex)
	if successor == input.OperationID {
		return successorRefuse(retained, "AGENT_RUNTIME_GENERATION_CONFLICT", "Agent successor generation does not replace a different operation")
	}
	if input.ExistingSuccessor != "" && input.ExistingSuccessor != successor {
		return successorRefuse(retained, "AGENT_RUNTIME_GENERATION_CONFLICT", "Agent successor generation does not replace a different operation")
	}
	return SuccessorDecision{
		Admitted: true, RetainedOperationID: input.OperationID, SuccessorOperationID: successor,
		Dispatch: "successor", Replayed: input.ExistingSuccessor == successor,
	}
}

// SuccessorBindingDecision is the persist result. The original operation id is
// never replaced. A refusal leaves any previously stored successor untouched.
type SuccessorBindingDecision struct {
	Persisted            bool
	ReplacedOperationID  bool
	OperationID          string
	SuccessorOperationID string
	ResetIndex           int64
	Code                 string
	Replayed             bool
}

// SuccessorBinding keeps the minted id beside the original operation.
func SuccessorBinding(input SuccessorInput) SuccessorBindingDecision {
	decision := SuccessorGeneration(input)
	if !decision.Admitted {
		return SuccessorBindingDecision{OperationID: decision.RetainedOperationID, SuccessorOperationID: input.ExistingSuccessor, Code: decision.Code}
	}
	return SuccessorBindingDecision{
		Persisted: true, OperationID: decision.RetainedOperationID, SuccessorOperationID: decision.SuccessorOperationID,
		ResetIndex: input.ResetIndex, Replayed: decision.Replayed,
	}
}

// SuccessorExecutionInput asks whether a persisted successor may run.
// AuthorityEpoch is the epoch claim or takeover already wrote. PresentedEpoch
// is the fence the caller wants to use. The worker must not promote itself.
type SuccessorExecutionInput struct {
	IdentityValid     bool
	Lease             string
	Epoch             string
	Effect            string
	ResetAdmitted     bool
	ResetFenceMatches bool
	OperationID       string
	ActionID          string
	ExecutionEpoch    uint64
	NodeID            string
	ResetIndex        int64
	StoredSuccessor   string
	StoredOperationID string
	AuthorityIssued   bool
	WorkerPromoted    bool
	AuthorityEpoch    uint64
	PresentedEpoch    uint64
}

// SuccessorExecutionDecision carries the operation id and epoch of one legal
// execution. Both are empty when execution is refused.
type SuccessorExecutionDecision struct {
	Admitted            bool
	Execute             bool
	Code                string
	Message             string
	RetainedOperationID string
	RetainedEpoch       uint64
	OperationID         string
	ExecutionEpoch      uint64
	Promoted            bool
}

func executionRefuse(retained string, retainedEpoch uint64, code, message string) SuccessorExecutionDecision {
	return SuccessorExecutionDecision{Code: code, Message: message, RetainedOperationID: retained, RetainedEpoch: retainedEpoch}
}

// SuccessorExecution admits one provider call for a persisted successor after
// claim or takeover has issued a newer epoch. EFFECT_UNKNOWN, a stale fence,
// and a worker that presents its own larger epoch do not execute.
func SuccessorExecution(input SuccessorExecutionInput) SuccessorExecutionDecision {
	retained := input.OperationID
	retainedEpoch := input.ExecutionEpoch
	if input.WorkerPromoted {
		return executionRefuse(retained, retainedEpoch, "FENCE_MISMATCH", "Worker cannot promote its own execution epoch")
	}
	if !input.IdentityValid {
		return executionRefuse(retained, retainedEpoch, "INVALID_IDENTITY", "Agent runtime identity is invalid")
	}
	if input.Lease != "live" {
		return executionRefuse(retained, retainedEpoch, "LEASE_NOT_HELD", "Agent runtime lease is not live")
	}
	if input.Epoch == "stale" || (input.AuthorityEpoch > 0 && input.PresentedEpoch < input.AuthorityEpoch) {
		return executionRefuse(retained, retainedEpoch, "STALE_EXECUTION_EPOCH", "Agent runtime epoch is stale")
	}
	if input.Epoch != "match" || input.AuthorityEpoch == 0 || input.PresentedEpoch == 0 || input.PresentedEpoch != input.AuthorityEpoch {
		return executionRefuse(retained, retainedEpoch, "FENCE_MISMATCH", "Agent runtime fence does not match")
	}
	if input.Effect == "AWAITING_TOOLS" {
		return executionRefuse(retained, retainedEpoch, "AGENT_DRIVER_TOOLS_PENDING", "Tool continuation stays on the original effect")
	}
	if input.Effect != "CONFIRMED" && input.Effect != "SETTLED_FAILURE" {
		return executionRefuse(retained, retainedEpoch, "EFFECT_UNKNOWN", "Agent runtime effect is unknown and was not retried")
	}
	if !input.ResetAdmitted || !input.ResetFenceMatches {
		return executionRefuse(retained, retainedEpoch, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "Agent reset requires a settled effect on the same fence")
	}
	if !input.AuthorityIssued {
		return executionRefuse(retained, retainedEpoch, "FENCE_MISMATCH", "Only claim or takeover can issue the next execution epoch")
	}
	if !canonicalToken(input.OperationID, 256) || !canonicalToken(input.ActionID, 256) || !canonicalToken(input.NodeID, 64) || input.ExecutionEpoch == 0 || input.ResetIndex < 0 {
		return executionRefuse(retained, retainedEpoch, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "Agent reset requires a settled effect on the same fence")
	}
	if input.StoredOperationID != input.OperationID {
		return executionRefuse(retained, retainedEpoch, "AGENT_RUNTIME_GENERATION_CONFLICT", "Agent successor generation does not replace a different operation")
	}
	if input.AuthorityEpoch <= input.ExecutionEpoch {
		return executionRefuse(retained, retainedEpoch, "AGENT_RUNTIME_SUCCESSOR_HELD", "Successor generation stays on the original fence until takeover")
	}
	successor := SuccessorOperationID(input.OperationID, input.ActionID, input.ExecutionEpoch, input.NodeID, input.ResetIndex)
	if input.StoredSuccessor == "" || input.StoredSuccessor != successor || input.StoredSuccessor == input.OperationID {
		return executionRefuse(retained, retainedEpoch, "AGENT_RUNTIME_SUCCESSOR_HELD", "Successor generation is not persisted on the original intent")
	}
	return SuccessorExecutionDecision{
		Admitted: true, Execute: true, RetainedOperationID: input.OperationID, RetainedEpoch: input.ExecutionEpoch,
		OperationID: input.StoredSuccessor, ExecutionEpoch: input.AuthorityEpoch,
	}
}

// DispatchAllowed classifies a retained node effect before a new provider call.
// An unknown effect is not a missing effect and must not be dispatched again.
// A tool wait stays on the original effect.
func DispatchAllowed(lease, epoch, effect string) (string, bool) {
	if lease != "live" {
		return "LEASE_NOT_HELD", false
	}
	if epoch == "stale" {
		return "STALE_EXECUTION_EPOCH", false
	}
	if epoch != "match" {
		return "FENCE_MISMATCH", false
	}
	switch effect {
	case "EFFECT_UNKNOWN", "":
		return "EFFECT_UNKNOWN", false
	case "AWAITING_TOOLS":
		return "AGENT_DRIVER_TOOLS_PENDING", false
	case "CONFIRMED", "SETTLED_FAILURE", "NONE":
		return "", true
	default:
		return "EFFECT_UNKNOWN", false
	}
}

const retainedToolResultScope = "retained_tool_results"

// DriverToolDecision is the scheduling copy of tool results already stored for
// one waiting effect. Returned events are headers. Result bytes stay in Rust.
type DriverToolDecision struct {
	Returned bool
	Code     string
	Events   []RunEvent
}

func driverBodyRef(ref ArtifactRef) bool {
	if len(ref.SHA256) != 64 || ref.Length == 0 {
		return false
	}
	for _, character := range ref.SHA256 {
		switch {
		case character >= '0' && character <= '9':
		case character >= 'a' && character <= 'f':
		default:
			return false
		}
	}
	return true
}

// RetainedDriverToolResults returns scheduling copies of tool results already
// stored for a waiting effect. A request marker must name the same public
// artifact. This does not call the tool and does not settle the node. A
// waiting effect with no stored result stays pending.
func RetainedDriverToolResults(state string, public ArtifactRef, events []RunEvent) DriverToolDecision {
	pending := DriverToolDecision{Code: "AGENT_DRIVER_TOOLS_PENDING"}
	if state != "AWAITING_TOOLS" && state != "awaiting_tools" {
		return DriverToolDecision{}
	}
	if !driverBodyRef(public) {
		return pending
	}
	phase := ""
	marked := false
	for _, event := range events {
		if event.Type != "agent_tool_request" || event.Scope != retainedToolResultScope {
			continue
		}
		if event.Body.SHA256 != public.SHA256 || event.Body.Length != public.Length || event.Phase == "" {
			continue
		}
		phase = event.Phase
		marked = true
	}
	if !marked {
		return pending
	}
	fresh := []RunEvent{}
	seen := map[string]bool{}
	for _, event := range events {
		if event.Type != "agent_tool_result" || event.Phase != phase || !driverBodyRef(event.Body) {
			continue
		}
		key := event.Phase + "\x00" + event.Scope + "\x00" + event.Body.SHA256
		if seen[key] {
			continue
		}
		seen[key] = true
		fresh = append(fresh, event)
	}
	if len(fresh) == 0 {
		return pending
	}
	return DriverToolDecision{Returned: true, Events: fresh}
}

const journalStateDocument = `{"type":"node_effect","phase":"%s","status":"%s","state":"%s"}`
const journalObservationTemplate = `{"type":"node_effect","phase":"%s","scope":"%s","observation":"%s"}`

// JournalObservation is the public node_effect projection of one journal read.
// Append is false for a settled read so the success path does not move the cursor.
type JournalObservation struct {
	Kind   string
	Append bool
	Phase  string
	Status string
	Scope  string
	SHA256 string
	Length uint64
}

func safeJournalToken(value string) bool {
	if len(value) > 64 {
		return false
	}
	for _, character := range value {
		if character > 127 {
			return false
		}
		switch {
		case character >= '0' && character <= '9':
		case character >= 'A' && character <= 'Z':
		case character >= 'a' && character <= 'z':
		case character == '_' || character == '.' || character == ':' || character == '-':
		default:
			return false
		}
	}
	return true
}

func journalObservationScope(scope string) bool {
	switch scope {
	case "missing", "timeout", "absent_log", "unavailable", "not_applied":
		return true
	default:
		return false
	}
}

func applyJournalTemplate(template string, values ...string) string {
	text := template
	for _, value := range values {
		text = strings.Replace(text, "%s", value, 1)
	}
	return text
}

func nodeEffectBytes(phase, status, scope string) ([]byte, bool) {
	if !safeJournalToken(phase) || !safeJournalToken(status) || !safeJournalToken(scope) {
		return nil, false
	}
	var text string
	if journalObservationScope(scope) {
		text = applyJournalTemplate(journalObservationTemplate, phase, scope, scope)
	} else {
		text = applyJournalTemplate(journalStateDocument, phase, status, status)
	}
	return []byte(text), true
}

func journalObservationDocument(phase, status, scope string, append bool, kind string) JournalObservation {
	raw, ok := nodeEffectBytes(phase, status, scope)
	if !ok {
		if phase != "" || status != "" || scope != "unavailable" {
			return journalObservationDocument("", "", "unavailable", true, "unreadable")
		}
		return JournalObservation{}
	}
	digest := sha256.Sum256(raw)
	publicStatus := ""
	publicScope := ""
	if journalObservationScope(scope) {
		publicScope = scope
	} else {
		publicStatus = status
	}
	return JournalObservation{
		Kind: kind, Append: append, Phase: phase, Status: publicStatus, Scope: publicScope,
		SHA256: hex.EncodeToString(digest[:]), Length: uint64(len(raw)),
	}
}

func canonicalNodeID(nodeID string) bool {
	return nodeID != "" && safeJournalToken(nodeID)
}

func journalQueryObservation(queryErr error) (string, bool) {
	if queryErr == nil {
		return "", false
	}
	if errors.Is(queryErr, context.DeadlineExceeded) || errors.Is(queryErr, context.Canceled) {
		return "timeout", true
	}
	text := queryErr.Error()
	lowered := strings.ToLower(text)
	if text == "timeout" || strings.Contains(lowered, "deadline exceeded") || strings.Contains(lowered, "deadlineexceeded") ||
		strings.Contains(lowered, "context canceled") || strings.Contains(lowered, "context cancelled") || strings.Contains(lowered, "timeout") {
		return "timeout", true
	}
	if strings.Contains(text, "ABSENT_LOG") || strings.Contains(lowered, "absent_log") {
		return "absent_log", true
	}
	return "unavailable", true
}

// ProjectJournalObservation maps one query result onto the public event stream.
// A missing row, a timeout, and an absent log are observations. They are not
// emitted as an unapplied state. Only unknown and waiting reads ask to be appended.
func ProjectJournalObservation(nodeID string, found bool, state string, queryErr error) JournalObservation {
	if !canonicalNodeID(nodeID) {
		return journalObservationDocument("", "", "unavailable", true, "unreadable")
	}
	if observation, failed := journalQueryObservation(queryErr); failed {
		return journalObservationDocument(nodeID, "", observation, true, "unreadable")
	}
	if !found {
		return journalObservationDocument(nodeID, "", "missing", true, "unreadable")
	}
	switch state {
	case "NOT_APPLIED", "not_applied":
		return journalObservationDocument(nodeID, "", "not_applied", true, "unreadable")
	case "AWAITING_TOOLS", "awaiting_tools":
		return journalObservationDocument(nodeID, "AWAITING_TOOLS", "", true, "awaiting")
	case "CONFIRMED", "confirmed", "SETTLED_FAILURE", "settled_failure", "NONE", "none":
		settled := state
		switch state {
		case "confirmed":
			settled = "CONFIRMED"
		case "settled_failure":
			settled = "SETTLED_FAILURE"
		case "none":
			settled = "NONE"
		}
		return journalObservationDocument(nodeID, settled, "", false, "settled")
	case "EFFECT_UNKNOWN", "effect_unknown":
		return journalObservationDocument(nodeID, "EFFECT_UNKNOWN", "", true, "unknown")
	case "DISPATCHING", "dispatching":
		return journalObservationDocument(nodeID, "DISPATCHING", "", true, "unknown")
	case "":
		return journalObservationDocument(nodeID, "", "", true, "unknown")
	default:
		if safeJournalToken(state) {
			return journalObservationDocument(nodeID, state, "", true, "unknown")
		}
		return journalObservationDocument(nodeID, "", "unavailable", true, "unreadable")
	}
}
