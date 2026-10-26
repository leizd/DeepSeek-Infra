package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

type mediaPatchWorker interface {
	ExecuteMediaPatch(context.Context, *mediav1.ExecuteMediaPatchRequest) (*mediav1.MediaPatchOutcome, error)
	QueryMediaPatch(context.Context, *mediav1.QueryMediaPatchRequest) (*mediav1.QueryMediaPatchResponse, error)
}

func dialMediaWorker() (*worker.Client, error) {
	cfg, err := worker.TLSDialConfigFromEnv()
	if err != nil {
		return nil, err
	}
	host, _, err := net.SplitHostPort(cfg.Target)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		return nil, worker.ErrWorkerTLSConfigInvalid
	}
	return worker.DialTLS(cfg)
}
func mediaUnknown(w http.ResponseWriter, operation string) {
	if operation != "" {
		w.Header().Set("X-DeepSeek-Operation-Id", operation)
	}
	policyWriteError(w, 503, "EFFECT_UNKNOWN", "Media update requires reconciliation; do not repeat the write")
}
func recoverMediaPatch(ctx context.Context, control *store.Control, client mediaPatchWorker, claim *mediav1.MediaPatchClaim) (*mediav1.MediaPatchOutcome, bool) {
	if claim == nil || claim.Intent == nil || claim.Fence == nil {
		return nil, false
	}
	response, err := client.QueryMediaPatch(ctx, &mediav1.QueryMediaPatchRequest{SchemaVersion: 1, OperationId: claim.Intent.OperationId, OriginalFence: claim.Fence})
	if err != nil || response == nil {
		return nil, false
	}
	if !response.Found {
		// Only the durable Go pre-dispatch state, transactionally cancelled by
		// its current writer, proves no future dispatch is possible. Worker
		// absence alone cannot release a dispatched/uncertain operation.
		if claim.State == "CLAIMED" && time.Now().Unix() >= claim.LeaseUntilUnix && control.CancelUndispatchedMediaPatch(claim.Intent.OperationId) == nil {
			return nil, true
		}
		return nil, false
	}
	if response.Outcome == nil {
		return nil, false
	}
	if _, err := control.RecordMediaPatchOutcome(response.Outcome); err != nil {
		return nil, false
	}
	return response.Outcome, true
}
func writeMediaOutcome(w http.ResponseWriter, request *http.Request, outcome *mediav1.MediaPatchOutcome) {
	w.Header().Set("X-DeepSeek-Operation-Id", outcome.OperationId)
	if outcome.State == mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED {
		policyWriteError(w, int(outcome.HttpStatus), outcome.ErrorCode, outcome.ErrorMessage)
		return
	}
	value, err := mediamodel.ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: outcome.Media}})
	if err != nil {
		mediaUnknown(w, outcome.OperationId)
		return
	}
	writeJSON(w, request, http.MethodPatch, map[string]any{"ok": true, "media": value})
}
func mediaPatch(w http.ResponseWriter, r *http.Request, control *store.Control, view PublicView) {
	if r.Method != http.MethodPatch {
		notImplemented(w, r)
		return
	}
	if !allowedPublicHost(r) {
		policyWriteError(w, 403, "forbidden", "Host not allowed")
		return
	}
	if !authorizedPublicRequest(r) {
		policyWriteError(w, 401, "unauthorized", "Auth required")
		return
	}
	if control == nil || view.Mode != config.ModeAuthoritative || view.MutationAuthority != config.MutationAuthorityGo || os.Getenv("DEEPSEEK_RUNTIME_MODE") != "python_disabled" {
		policyWriteError(w, 503, "MEDIA_AUTHORITY_UNAVAILABLE", "Native media authority unavailable")
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/api/media/")
	if id == "" || strings.Contains(id, "/") {
		notImplemented(w, r)
		return
	}
	if r.ContentLength <= 0 {
		policyWriteError(w, 400, "invalid_payload", "Request body is empty")
		return
	}
	if r.ContentLength > 1_000_000 {
		policyWriteError(w, 413, "upload_too_large", "Request body is too large")
		return
	}
	body := http.MaxBytesReader(w, r.Body, 1_000_000)
	decoder := json.NewDecoder(body)
	decoder.UseNumber()
	var payload any
	if err := decoder.Decode(&payload); err != nil {
		policyWriteError(w, 400, "invalid_payload", "Invalid JSON body")
		return
	}
	if decoder.Decode(new(any)) != io.EOF {
		policyWriteError(w, 400, "invalid_payload", "Invalid JSON body")
		return
	}
	fields, ok := payload.(map[string]any)
	if !ok {
		policyWriteError(w, 400, "invalid_payload", "Request body must be a JSON object")
		return
	}
	patch := &mediav1.MediaPatch{}
	present := false
	for _, field := range []string{"title", "projectId", "metadata"} {
		value, exists := fields[field]
		if !exists {
			continue
		}
		present = true
		typed, err := mediamodel.FromJSON(value)
		if err != nil {
			policyWriteError(w, 400, "invalid_payload", "Invalid media patch")
			return
		}
		switch field {
		case "title":
			patch.Title = typed
		case "projectId":
			patch.ProjectId = typed
		case "metadata":
			patch.Metadata = typed
		}
	}
	if !present {
		policyWriteError(w, 400, "invalid_payload", "Media patch did not include any supported fields")
		return
	}
	client, err := dialMediaWorker()
	if err != nil {
		policyWriteError(w, 503, "MEDIA_WORKER_UNAVAILABLE", "Native media worker unavailable")
		return
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	pending, err := control.PendingMediaPatch()
	if err != nil {
		mediaUnknown(w, "")
		return
	}
	if pending != nil {
		if _, settled := recoverMediaPatch(ctx, control, client, pending); !settled {
			mediaUnknown(w, pending.Intent.OperationId)
			return
		}
	}
	var nonce [16]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		policyWriteError(w, 503, "MEDIA_ID_UNAVAILABLE", "Media operation identity unavailable")
		return
	}
	operation := "media-patch-" + hex.EncodeToString(nonce[:])
	intent := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: operation, MediaId: id, Patch: patch}
	claim, err := control.AdmitMediaPatch(intent)
	if err != nil {
		mediaUnknown(w, operation)
		return
	}
	outcome, err := client.ExecuteMediaPatch(ctx, &mediav1.ExecuteMediaPatchRequest{SchemaVersion: 1, OperationId: operation, Fence: claim.Fence})
	if err == nil && outcome != nil {
		if _, recordErr := control.RecordMediaPatchOutcome(outcome); recordErr == nil {
			writeMediaOutcome(w, r, outcome)
			return
		}
	}
	// A failed response is ambiguous. Query the original operation once; never
	// execute again, mint another action or infer non-application from absence.
	queryCtx, queryCancel := context.WithTimeout(context.WithoutCancel(r.Context()), 5*time.Second)
	defer queryCancel()
	if retained, settled := recoverMediaPatch(queryCtx, control, client, claim); settled && retained != nil {
		writeMediaOutcome(w, r, retained)
		return
	}
	mediaUnknown(w, operation)
}
