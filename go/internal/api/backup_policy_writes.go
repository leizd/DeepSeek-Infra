package api

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/policy"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// maximumPolicyBodyBytes mirrors the Python route's `read_json_body(max_bytes=64_000)`.
const maximumPolicyBodyBytes = 64_000

// The refusal codes this surface adds to the read route's.
const (
	// CodeControlNotAuthoritative — the policy domain has not completed its native
	// cutover, so there is nowhere to write.
	CodeControlNotAuthoritative = "GO_CONTROL_NOT_AUTHORITATIVE"
	// CodeTargetRegistryUnavailable — a policy names a registered target and the
	// authoritative target inventory could not be read, so the binding cannot be verified.
	CodeTargetRegistryUnavailable = "GO_CONTROL_TARGET_REGISTRY_UNAVAILABLE"
)

// policyWriteError is the oracle's `AppError.to_response()` envelope.
func policyWriteError(writer http.ResponseWriter, status int, code string, message string) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": message, "code": code})
}

// normalizeRefusal maps a `internal/policy` failure onto the oracle's HTTP answer: an
// `AppError` keeps its own status and code, and an `UncaughtError` is the 500 the oracle's
// unhandled `ValueError` produces — not a 400, which would tell the client its payload was
// merely malformed.
func normalizeRefusal(writer http.ResponseWriter, err error) {
	var appError *policy.AppError
	if errors.As(err, &appError) {
		policyWriteError(writer, appError.Status, appError.Code, appError.Message)
		return
	}
	var uncaught *policy.UncaughtError
	if errors.As(err, &uncaught) {
		policyWriteError(writer, http.StatusInternalServerError, "internal", uncaught.Message)
		return
	}
	policyWriteError(writer, http.StatusInternalServerError, "internal", err.Error())
}

// writeRefusal maps a store failure onto the oracle's HTTP answer. A store that has not
// been promoted is a 503 with the read route's own code, so a browser sees the same
// "not native yet" answer for reads and writes; everything else keeps its name.
func writeRefusal(writer http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrCutoverNotAuthorized), errors.Is(err, store.ErrDomainNotAuthoritative):
		policyWriteError(writer, http.StatusServiceUnavailable, CodeControlNotAuthoritative,
			"Backup policies have not completed native ownership cutover")
	case errors.Is(err, store.ErrRevisionConflict):
		policyWriteError(writer, http.StatusConflict, "revision_conflict",
			"Backup policy changed since it was read; retry")
	case errors.Is(err, store.ErrIllegalTransition):
		policyWriteError(writer, http.StatusConflict, "illegal_transition",
			"Backup policy cannot move to that state")
	case errors.Is(err, store.ErrMutationRequestDomainFenced):
		policyWriteError(writer, http.StatusConflict, "mutation_request_domain_fenced",
			"this control domain's mutations belong to the lease and admission path")
	case errors.Is(err, store.ErrMutationRequestReplayConflict):
		policyWriteError(writer, http.StatusConflict, "mutation_request_replay_conflict",
			"operation id was already used for a different mutation")
	case errors.Is(err, store.ErrWriterFenceHeld), errors.Is(err, store.ErrSchemaInactive):
		policyWriteError(writer, http.StatusServiceUnavailable, "GO_CONTROL_UNAVAILABLE",
			"Go control store is not writable right now")
	default:
		policyWriteError(writer, http.StatusInternalServerError, "GO_CONTROL_WRITE_FAILED",
			"Backup policy write failed")
	}
}

// readPolicyBody mirrors `read_json_body`: a missing or unparsable body is an empty
// object, and only an oversized one is refused.
func readPolicyBody(writer http.ResponseWriter, request *http.Request) (map[string]any, bool) {
	raw, err := io.ReadAll(io.LimitReader(request.Body, maximumPolicyBodyBytes+1))
	if err != nil {
		policyWriteError(writer, http.StatusBadRequest, "invalid_payload", "Request body is unreadable")
		return nil, false
	}
	if len(raw) > maximumPolicyBodyBytes {
		policyWriteError(writer, http.StatusRequestEntityTooLarge, "request_too_large",
			"Backup policy payload is too large")
		return nil, false
	}
	if len(strings.TrimSpace(string(raw))) == 0 {
		return map[string]any{}, true
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	var payload map[string]any
	if err := decoder.Decode(&payload); err != nil || payload == nil {
		policyWriteError(writer, http.StatusBadRequest, "invalid_payload", "Request body must be a JSON object")
		return nil, false
	}
	return payload, true
}

// nowISOMatchesPolicy reproduces `_now_iso()`: UTC, second precision, `Z` suffix.
func nowISOMatchesPolicy() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05Z")
}

// newPolicyID mirrors `f"policy_{secrets.token_hex(8)}"`.
func newPolicyID() string {
	var raw [8]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return ""
	}
	return "policy_" + hex.EncodeToString(raw[:])
}

// newActionID is the identity this write is admitted under. It is generated server-side:
// a caller cannot supply the action it wants to be judged by.
func newActionID() string {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return ""
	}
	return "action_" + hex.EncodeToString(raw[:])
}

// newOperationID is the idempotency key. The browser does not send one, so the route
// derives it from the request's own identity: the same request replayed within a retry
// window is answered from the journal instead of writing twice. Two genuinely different
// writes never collide because the id carries fresh entropy.
func newOperationID() string {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return ""
	}
	return "op_" + hex.EncodeToString(raw[:])
}

// registeredTargets reads the authoritative target inventory.
//
// A policy may name a registered `target_...`; the oracle refuses to store a binding to a
// target that does not exist, so this route has to answer the same question. The
// authoritative inventory is Go's, and a store that does not own the target domain yet
// cannot answer it — so the write is refused rather than accepted unvalidated.
func registeredTargets(control *store.Control) (map[string]bool, error) {
	records, _, err := control.ListAuthoritativeTargets()
	if err != nil {
		return nil, err
	}
	targets := make(map[string]bool, len(records))
	for _, record := range records {
		targets[record.ID] = true
	}
	return targets, nil
}

// validatePolicyTargetBindings mirrors `backup_policies.validate_target_bindings`.
//
// The registry is read **lazily**, exactly as the oracle does: it calls `get_target` only
// for an id that is neither `managed-local` nor `unbound`, so a policy that names no
// registered target does not depend on the target domain having completed its own
// cutover. `resolve` returns the error a store read produced, and the caller turns that
// into a refusal rather than skipping the check.
func validatePolicyTargetBindings(
	document map[string]any,
	resolve func() (map[string]bool, error),
) error {
	needsRegistry := func(targetID string) bool {
		return targetID != "" && targetID != policy.ManagedLocalTarget && targetID != policy.UnboundTarget
	}
	primary, _ := document["targetId"].(string)
	replicas := []string{}
	replication, _ := document["replication"].(map[string]any)
	entries, _ := replication["targets"].([]any)
	for _, entry := range entries {
		replica, _ := entry.(map[string]any)
		if targetID, _ := replica["targetId"].(string); needsRegistry(targetID) {
			replicas = append(replicas, targetID)
		}
	}
	if !needsRegistry(primary) && len(replicas) == 0 {
		return nil
	}
	targets, err := resolve()
	if err != nil {
		return err
	}
	if needsRegistry(primary) && !targets[primary] {
		return &policy.AppError{
			Message: fmt.Sprintf("Unregistered primary targetId '%s'", primary),
			Code:    policy.CodeInvalidPayload,
			Status:  http.StatusBadRequest,
		}
	}
	for _, targetID := range replicas {
		if !targets[targetID] {
			return &policy.AppError{
				Message: fmt.Sprintf("Unregistered replica targetId '%s'", targetID),
				Code:    policy.CodeInvalidPayload,
				Status:  http.StatusBadRequest,
			}
		}
	}
	return nil
}

// refuseTargetBindings turns a registry failure into the route's named refusal and an
// unregistered target into the oracle's own `invalid_payload` message.
func refuseTargetBindings(writer http.ResponseWriter, err error) {
	var appError *policy.AppError
	if errors.As(err, &appError) {
		normalizeRefusal(writer, err)
		return
	}
	policyWriteError(writer, http.StatusServiceUnavailable, CodeTargetRegistryUnavailable,
		"The authoritative target registry is unavailable, so a policy binding cannot be verified")
}

// policyRecordState maps the policy document onto the control store's state column.
// The oracle's policy rows have no state; this store's transitions are ACTIVE/DISABLED,
// and `enabled` is the same fact.
func policyRecordState(document map[string]any) string {
	if enabled, ok := document["enabled"].(bool); ok && enabled {
		return "ACTIVE"
	}
	return "DISABLED"
}

// policyRecordRevision reads the CAS revision `normalize_policy` put in the document.
func policyRecordRevision(document map[string]any) int64 {
	switch typed := document["policyRevision"].(type) {
	case json.Number:
		if parsed, err := typed.Int64(); err == nil {
			return parsed
		}
	case int64:
		return typed
	case float64:
		return int64(typed)
	}
	return 1
}

// authorizationAndHost applies the two admission checks every public control route shares.
func authorizationAndHost(writer http.ResponseWriter, request *http.Request) bool {
	if !allowedPublicHost(request) {
		backupPolicyError(writer, http.StatusForbidden, "forbidden", "Host not allowed")
		return false
	}
	if !authorizedPublicRequest(request) {
		backupPolicyError(writer, http.StatusUnauthorized, "unauthorized", "Auth required")
		return false
	}
	return true
}

// backupPoliciesCreate serves `POST /api/workspace/backup-policies`.
func backupPoliciesCreate(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if !authorizationAndHost(writer, request) {
		return
	}
	if control == nil {
		policyWriteError(writer, http.StatusServiceUnavailable, "CONTROL_UNAVAILABLE", "Go control store unavailable")
		return
	}
	payload, ok := readPolicyBody(writer, request)
	if !ok {
		return
	}
	policyID := ""
	if text, isText := payload["policyId"].(string); isText {
		policyID = strings.TrimSpace(text)
	}
	if policyID == "" {
		policyID = newPolicyID()
		if policyID == "" {
			policyWriteError(writer, http.StatusInternalServerError, "internal", "Unable to allocate a policy id")
			return
		}
	}
	now := nowISOMatchesPolicy()
	document, err := policy.NormalizePolicy(payload, policyID, now, now)
	if err != nil {
		normalizeRefusal(writer, err)
		return
	}
	if err := validatePolicyTargetBindings(document, func() (map[string]bool, error) {
		return registeredTargets(control)
	}); err != nil {
		refuseTargetBindings(writer, err)
		return
	}
	// This store creates a record at revision 1 and advances it by one per write; the
	// attested inventory import is the only path that adopts a higher baseline revision
	// (`allowImportedBaseline`), and it carries source provenance this route does not
	// have. A create asking for a later revision is therefore refused by name instead of
	// being silently rewritten — the oracle would store it, so the narrowing is recorded
	// in `release/native_runtime_go_control_store_v1.json`.
	if revision := policyRecordRevision(document); revision != 1 {
		policyWriteError(writer, http.StatusBadRequest, "invalid_payload",
			fmt.Sprintf("Backup policy create must start at policyRevision 1, not %d", revision))
		return
	}
	encoded, err := json.Marshal(document)
	if err != nil {
		policyWriteError(writer, http.StatusInternalServerError, "internal", "Unable to encode the policy")
		return
	}
	result, err := control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: newOperationID(),
		ActionID:    newActionID(),
		Actor:       "operator:public-api",
		Record: store.Record{
			Domain:   "policy",
			ID:       policyID,
			Revision: policyRecordRevision(document),
			State:    policyRecordState(document),
			Payload:  encoded,
		},
	})
	if err != nil {
		if errors.Is(err, store.ErrIllegalTransition) || errors.Is(err, store.ErrRevisionConflict) {
			// A create whose id already exists is the oracle's 409 collision, not a CAS
			// refusal: the caller asked for a new object and the id is taken.
			policyWriteError(writer, http.StatusConflict, "invalid_request", "Backup policy id collision; retry")
			return
		}
		writeRefusal(writer, err)
		return
	}
	_ = result
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(document)
}

// policyPatchFields is the oracle's `update_policy` merge list, verbatim. A field outside
// it is ignored rather than stored, which is what keeps a client's stray key from becoming
// durable state.
var policyPatchFields = []string{
	"name", "enabled", "schedule", "scope", "frontendMirror", "protection", "targetId",
	"primaryTargetId", "policyRevision", "retentionPolicyId", "retry", "incremental",
	"recoveryObjectives", "costObjectives", "recoveryDrill", "replication",
	"federatedDurability", "placement",
}

// backupPoliciesUpdate serves `PATCH /api/workspace/backup-policies/{policy_id}`.
func backupPoliciesUpdate(writer http.ResponseWriter, request *http.Request, profileID string, control *store.Control) {
	if !authorizationAndHost(writer, request) {
		return
	}
	if control == nil {
		policyWriteError(writer, http.StatusServiceUnavailable, "CONTROL_UNAVAILABLE", "Go control store unavailable")
		return
	}
	patch, ok := readPolicyBody(writer, request)
	if !ok {
		return
	}
	existing, exists, err := control.Get("policy", profileID)
	if err != nil {
		if errors.Is(err, store.ErrDomainNotAuthoritative) {
			policyWriteError(writer, http.StatusServiceUnavailable, CodeControlNotAuthoritative,
				"Backup policies have not completed native ownership cutover")
			return
		}
		policyWriteError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup policy store read failed")
		return
	}
	if !exists || existing.State == store.TombstoneState {
		policyWriteError(writer, http.StatusNotFound, "not_found", "Backup policy not found")
		return
	}
	var current map[string]any
	decoder := json.NewDecoder(strings.NewReader(string(existing.Payload)))
	decoder.UseNumber()
	if err := decoder.Decode(&current); err != nil || current == nil {
		policyWriteError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup policy payload invalid")
		return
	}
	merged := make(map[string]any, len(current))
	for key, value := range current {
		merged[key] = value
	}
	for _, field := range policyPatchFields {
		if value, present := patch[field]; present {
			merged[field] = value
		}
	}
	// The revision is the store's to advance, not the patch's.
	nextRevision := existing.Revision + 1
	merged["policyRevision"] = json.Number(fmt.Sprintf("%d", nextRevision))
	createdAt, _ := current["createdAt"].(string)
	document, err := policy.NormalizePolicy(merged, existing.ID, createdAt, nowISOMatchesPolicy())
	if err != nil {
		normalizeRefusal(writer, err)
		return
	}
	if err := validatePolicyTargetBindings(document, func() (map[string]bool, error) {
		return registeredTargets(control)
	}); err != nil {
		refuseTargetBindings(writer, err)
		return
	}
	encoded, err := json.Marshal(document)
	if err != nil {
		policyWriteError(writer, http.StatusInternalServerError, "internal", "Unable to encode the policy")
		return
	}
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: newOperationID(),
		ActionID:    newActionID(),
		Actor:       "operator:public-api",
		Record: store.Record{
			Domain:   "policy",
			ID:       existing.ID,
			Revision: nextRevision,
			State:    policyRecordState(document),
			Payload:  encoded,
		},
	}); err != nil {
		writeRefusal(writer, err)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(document)
}

// backupPoliciesDelete serves `DELETE /api/workspace/backup-policies/{policy_id}`.
//
// The oracle removes the row. This store cannot: the control event journal is append-only
// and immutable, and a record whose events outlive it makes every later read of that id
// fail closed with `CORRUPT_RECORD` ("orphaned control events"). A delete is therefore a
// **terminal tombstone**: the row moves to `DELETED` with the next revision and keeps its
// document, `Get` and the authoritative list both treat it as absent, and nothing can
// leave the state — which is what makes the id consumed rather than reusable.
//
// The response is the oracle's own: `{"deleted": true, "policyId": ...}`.
func backupPoliciesDelete(writer http.ResponseWriter, request *http.Request, policyID string, control *store.Control) {
	if !authorizationAndHost(writer, request) {
		return
	}
	if control == nil {
		policyWriteError(writer, http.StatusServiceUnavailable, "CONTROL_UNAVAILABLE", "Go control store unavailable")
		return
	}
	existing, exists, err := control.Get("policy", policyID)
	if err != nil {
		policyWriteError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup policy store read failed")
		return
	}
	// A tombstone reads as absent, exactly as a removed row would.
	if !exists || existing.State == store.TombstoneState {
		policyWriteError(writer, http.StatusNotFound, "not_found", "Backup policy not found")
		return
	}
	var document map[string]any
	decoder := json.NewDecoder(strings.NewReader(string(existing.Payload)))
	decoder.UseNumber()
	if err := decoder.Decode(&document); err != nil || document == nil {
		policyWriteError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup policy payload invalid")
		return
	}
	// The tombstone keeps the document so an auditor can see what was removed; only the
	// revision moves, which is what the store's CAS payload rule requires.
	nextRevision := existing.Revision + 1
	document["policyRevision"] = json.Number(fmt.Sprintf("%d", nextRevision))
	encoded, err := json.Marshal(document)
	if err != nil {
		policyWriteError(writer, http.StatusInternalServerError, "internal", "Unable to encode the policy")
		return
	}
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: newOperationID(),
		ActionID:    newActionID(),
		Actor:       "operator:public-api",
		Record: store.Record{
			Domain:   "policy",
			ID:       existing.ID,
			Revision: nextRevision,
			State:    store.TombstoneState,
			Payload:  encoded,
		},
	}); err != nil {
		writeRefusal(writer, err)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(map[string]any{"deleted": true, "policyId": existing.ID})
}
