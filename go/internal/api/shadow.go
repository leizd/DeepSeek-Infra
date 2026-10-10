package api

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/internal/shadow"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// InternalOptions configures the internal control plane. The mutation signer key
// is deployment trust material: it is never read from a request, so a caller
// cannot nominate the signer that authorizes its own production mutation.
type InternalOptions struct {
	Bearer                  string
	MutationSignerPublicKey string
	FleetID                 string
	Environment             string
	// Now judges the request's issuedAt/expiresAt. It defaults to wall clock so a
	// store's test clock cannot make a real request look expired.
	Now func() time.Time
}

// A checkpoint can carry a full control inventory, so it has a larger bound
// than the signed 16 KiB mutation request. The bound still prevents an
// authenticated peer from streaming unbounded data into the control process.
const maxAuthorityCheckpointBytes = 16 << 20

// Register mounts the internal control plane behind bearer authentication.
// A blank bearer mounts the routes but refuses every request, so the control
// plane is never served unauthenticated by accident.
func Register(mux *http.ServeMux, control *store.Control, bearer string) {
	RegisterWithOptions(mux, control, InternalOptions{Bearer: bearer})
}

// RegisterWithOptions mounts the internal control plane with the full deployment
// configuration.
func RegisterWithOptions(mux *http.ServeMux, control *store.Control, options InternalOptions) {
	internal := http.NewServeMux()
	internal.HandleFunc("/internal/shadow/evaluate", func(writer http.ResponseWriter, request *http.Request) {
		evaluate(writer, request, control)
	})
	internal.HandleFunc("/internal/shadow/snapshot", func(writer http.ResponseWriter, request *http.Request) {
		snapshot(writer, request, control)
	})
	internal.HandleFunc("/internal/action/execute", deny)
	internal.HandleFunc("/internal/action/dispatch", func(writer http.ResponseWriter, request *http.Request) {
		dispatch(writer, request, control)
	})
	internal.HandleFunc("/internal/cutover/status", func(writer http.ResponseWriter, request *http.Request) {
		cutoverStatus(writer, request, control)
	})
	internal.HandleFunc("/internal/cutover/transition", func(writer http.ResponseWriter, request *http.Request) {
		cutoverTransition(writer, request, control)
	})
	internal.HandleFunc("/internal/authority/head", func(writer http.ResponseWriter, request *http.Request) {
		authorityHead(writer, request, control)
	})
	internal.HandleFunc("/internal/authority/claim", func(writer http.ResponseWriter, request *http.Request) {
		claimAuthority(writer, request, control)
	})
	internal.HandleFunc("/internal/mutation/apply", func(writer http.ResponseWriter, request *http.Request) {
		applyMutation(writer, request, control, options)
	})
	// The sealed frontend mirror's recipient source: the native edge seals a generation
	// per enabled policy, and the policy records are this control plane's.
	internal.HandleFunc("/internal/control/backup-policy-recipients", func(writer http.ResponseWriter, request *http.Request) {
		backupPolicyRecipients(writer, request, control)
	})
	mux.Handle("/internal/", RequireInternalBearer(internal, options.Bearer))
}

func authorityHead(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodGet {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writeInternalError(writer, http.StatusServiceUnavailable, "CONTROL_STORE_UNAVAILABLE")
		return
	}
	head, exists, err := control.ControlAuthorityHead()
	if err != nil {
		writeInternalError(writer, http.StatusConflict, err.Error())
		return
	}
	if !exists {
		writeInternalError(writer, http.StatusNotFound, "CONTROL_AUTHORITY_NOT_CLAIMED")
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(head)
}

// claimAuthority only transports a checkpoint to the store's existing
// integrity, chain, writer-lease and deployment-capability checks. This route
// cannot create a cutover or authorize itself; the same loopback bearer guard
// protects it as every other /internal/* control operation.
func claimAuthority(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodPost {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writeInternalError(writer, http.StatusServiceUnavailable, "CONTROL_STORE_UNAVAILABLE")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(request.Body, maxAuthorityCheckpointBytes+1))
	if err != nil {
		writeInternalError(writer, http.StatusBadRequest, "INVALID_AUTHORITY_CHECKPOINT")
		return
	}
	if len(raw) > maxAuthorityCheckpointBytes {
		writeInternalError(writer, http.StatusRequestEntityTooLarge, "AUTHORITY_CHECKPOINT_TOO_LARGE")
		return
	}
	var checkpoint *store.AuthorityCheckpoint
	if err := json.Unmarshal(raw, &checkpoint); err != nil || checkpoint == nil {
		writeInternalError(writer, http.StatusBadRequest, "INVALID_AUTHORITY_CHECKPOINT")
		return
	}
	head, advanced, err := control.ClaimControlAuthority(checkpoint)
	if err != nil {
		writeInternalError(writer, http.StatusConflict, err.Error())
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(struct {
		Head     store.AuthorityHead `json:"head"`
		Advanced bool                `json:"advanced"`
	}{Head: head, Advanced: advanced})
}

// applyMutation is the transport for the approved production-apply channel: the
// request body is the exact canonical control-mutation-request-v2 document, and
// the signer it must be signed by comes from deployment configuration.
func applyMutation(writer http.ResponseWriter, request *http.Request, control *store.Control, options InternalOptions) {
	if request.Method != http.MethodPost {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writeInternalError(writer, http.StatusServiceUnavailable, "CONTROL_STORE_UNAVAILABLE")
		return
	}
	// The apply route has no signer to trust until the deployment configures one,
	// so an unconfigured deployment refuses rather than accepting anything.
	if strings.TrimSpace(options.MutationSignerPublicKey) == "" {
		writeInternalError(writer, http.StatusServiceUnavailable, "MUTATION_SIGNER_NOT_CONFIGURED")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(request.Body, store.MaxMutationRequestBytes+1))
	if err != nil {
		writeInternalError(writer, http.StatusBadRequest, "MUTATION_REQUEST_UNREADABLE")
		return
	}
	if len(raw) > store.MaxMutationRequestBytes {
		writeInternalError(writer, http.StatusRequestEntityTooLarge, "MUTATION_REQUEST_TOO_LARGE")
		return
	}
	now := time.Now().UTC()
	if options.Now != nil {
		now = options.Now().UTC()
	}
	result, err := control.ApplyMutation(raw, store.MutationAuthority{
		SignerPublicKey: options.MutationSignerPublicKey,
		FleetID:         options.FleetID,
		Environment:     options.Environment,
		Now:             now,
	})
	writer.Header().Set("Content-Type", "application/json")
	if err != nil {
		writer.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": err.Error()})
		return
	}
	_ = json.NewEncoder(writer).Encode(result)
}

func writeInternalError(writer http.ResponseWriter, status int, code string) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": code})
}

// Handler serves the public qualification API with no internal control plane:
// every /internal/* request is refused.
func Handler() http.Handler {
	return HandlerWithBearer("")
}

// HandlerWithBearer serves the public qualification API plus the internal
// control plane authenticated by bearer.
func HandlerWithBearer(bearer string) http.Handler {
	mux := http.NewServeMux()
	Register(mux, nil, bearer)
	RegisterPublic(mux, nil)
	return mux
}

func evaluate(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodPost {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var snapshot map[string]any
	if err := json.NewDecoder(request.Body).Decode(&snapshot); err != nil {
		writer.WriteHeader(http.StatusBadRequest)
		return
	}
	decision, err := shadow.Evaluate(snapshot)
	if err != nil {
		writer.WriteHeader(http.StatusBadRequest)
		return
	}
	if err := shadow.Persist(control, snapshot, decision); err != nil {
		writer.WriteHeader(http.StatusConflict)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(decision)
}

func snapshot(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodGet {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writer.WriteHeader(http.StatusNotFound)
		return
	}
	report, err := control.ExportSnapshot()
	if err != nil {
		writer.WriteHeader(http.StatusConflict)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(report)
}

func dispatch(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodPost {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Kind           string `json:"kind"`
		ActionID       string `json:"actionId"`
		ExecutionEpoch uint64 `json:"executionEpoch"`
	}
	if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
		writer.WriteHeader(http.StatusBadRequest)
		return
	}
	kind, ok := internalprotocol.KindFromName(body.Kind)
	if !ok {
		writer.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": internalprotocol.ErrUnknownEffect.Error()})
		return
	}
	liveEpoch := uint64(0)
	if control != nil && body.ActionID != "" {
		record, exists, lookupErr := control.Get("action", body.ActionID)
		if lookupErr != nil {
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusConflict)
			_ = json.NewEncoder(writer).Encode(map[string]string{"error": internalprotocol.ErrFenceMismatch.Error()})
			return
		}
		if exists {
			liveEpoch = record.ExecutionEpoch
		}
	}
	err := internalprotocol.PlanNative(kind, &internalprotocol.ActionFence{ActionId: body.ActionID, ExecutionEpoch: body.ExecutionEpoch}, liveEpoch)
	writer.Header().Set("Content-Type", "application/json")
	status := http.StatusConflict
	if err == internalprotocol.ErrEmptyActionID || err == internalprotocol.ErrZeroEpoch || err == internalprotocol.ErrUnknownEffect {
		status = http.StatusBadRequest
	}
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": err.Error()})
}

func deny(writer http.ResponseWriter, _ *http.Request) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(http.StatusForbidden)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": internalprotocol.ErrMutationDenied.Error()})
}

func cutoverStatus(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodGet {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writer.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	domain := request.URL.Query().Get("domain")
	if domain == "" {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": "domain required"})
		return
	}
	rec, err := control.GetCutover(domain)
	writer.Header().Set("Content-Type", "application/json")
	if err != nil {
		writer.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": err.Error()})
		return
	}
	_ = json.NewEncoder(writer).Encode(rec)
}

func cutoverTransition(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodPost {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writer.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	var req store.CutoverTransition
	if err := json.NewDecoder(request.Body).Decode(&req); err != nil {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": "invalid json"})
		return
	}
	rec, err := control.TransitionCutover(req)
	writer.Header().Set("Content-Type", "application/json")
	if err != nil {
		writer.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(writer).Encode(map[string]string{"error": err.Error()})
		return
	}
	_ = json.NewEncoder(writer).Encode(rec)
}
