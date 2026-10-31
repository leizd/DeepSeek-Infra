package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
)

// http_utils.truthy stringifies JSON values before matching its four tokens.
// In particular JSON integer 1 is true, while JSON float 1.0 is not.
func mediaProcessTruthy(value any) bool {
	switch v := value.(type) {
	case bool:
		return v
	case string:
		return mediaUploadTruthy(v)
	case json.Number:
		return v.String() == "1"
	default:
		return false
	}
}

func parseMediaProcessOptions(w http.ResponseWriter, r *http.Request) (*mediav1.MediaProcessOptions, bool) {
	payload := map[string]any{}
	// Preserve the original route's deliberate empty/no-Content-Length case.
	if r.ContentLength > 0 {
		if r.ContentLength > 2_000_000 {
			policyWriteError(w, 413, "upload_too_large", "Request body is too large")
			return nil, false
		}
		decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2_000_000))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil || decoder.Decode(new(any)) != io.EOF {
			policyWriteError(w, 400, "invalid_payload", "Invalid JSON body")
			return nil, false
		}
		var ok bool
		payload, ok = value.(map[string]any)
		if !ok {
			policyWriteError(w, 400, "invalid_payload", "Request body must be a JSON object")
			return nil, false
		}
	}
	options := &mediav1.MediaProcessOptions{Force: mediaUploadTruthy(mediaUploadLast(r.URL.Query()["force"])) || mediaProcessTruthy(payload["force"])}
	if v, present := payload["ocrEnabled"]; present {
		b := mediaProcessTruthy(v)
		options.OcrEnabled = &b
	}
	if options.GetOcrEnabled() && truthy(payload["apiKey"]) {
		// Credential overrides require a separate Rust-custody transport. Never
		// put a caller's key in an immutable Go intent or silently ignore it.
		policyWriteError(w, 501, "MEDIA_OCR_CREDENTIAL_OVERRIDE_NOT_READY", "Native media OCR credential override is unavailable")
		return nil, false
	}
	correlation := pythonTrim(r.Header.Get("X-DeepSeek-Request-ID"))
	if len(correlation) > 64 || strings.IndexFunc(correlation, func(c rune) bool {
		return !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.ContainsRune("-_.:", c))
	}) >= 0 {
		correlation = ""
	}
	if correlation == "" {
		var nonce [16]byte
		if _, err := rand.Read(nonce[:]); err != nil {
			policyWriteError(w, 503, "MEDIA_ID_UNAVAILABLE", "Media request identity unavailable")
			return nil, false
		}
		correlation = hex.EncodeToString(nonce[:])
	}
	options.CorrelationId = correlation
	return options, true
}

func writeMediaProcessed(w http.ResponseWriter, r *http.Request, client *worker.Client, outcome *mediav1.MediaPatchOutcome) {
	w.Header().Set("X-DeepSeek-Operation-Id", outcome.OperationId)
	if outcome.State == mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED {
		policyWriteError(w, int(outcome.HttpStatus), outcome.ErrorCode, outcome.ErrorMessage)
		return
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), 10*time.Second)
	defer cancel()
	result, err := client.ReadMediaProcessResult(ctx, outcome)
	if err != nil {
		mediaUnknown(w, outcome.OperationId)
		return
	}
	if outcome.HttpStatus != http.StatusOK {
		policyWriteError(w, int(outcome.HttpStatus), outcome.ErrorCode, outcome.ErrorMessage)
		return
	}
	record, err := mediamodel.ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: result.Media}})
	if err != nil {
		mediaUnknown(w, outcome.OperationId)
		return
	}
	segments, err := mediamodel.ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ArrayValue{ArrayValue: result.Segments}})
	if err != nil {
		mediaUnknown(w, outcome.OperationId)
		return
	}
	writeJSON(w, r, http.MethodPost, map[string]any{"ok": true, "media": record, "segments": segments, "indexed": result.Indexed})
}

func mediaProcess(w http.ResponseWriter, r *http.Request, control *store.Control, view PublicView) {
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusMethodNotAllowed)
		_ = json.NewEncoder(w).Encode(map[string]any{"detail": "Method Not Allowed"})
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
	id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/media/"), "/process")
	if id == "" || strings.Contains(id, "/") {
		notImplemented(w, r)
		return
	}
	options, ok := parseMediaProcessOptions(w, r)
	if !ok {
		return
	}
	w.Header().Set("X-DeepSeek-Request-ID", options.CorrelationId)
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
	if _, err = rand.Read(nonce[:]); err != nil {
		policyWriteError(w, 503, "MEDIA_ID_UNAVAILABLE", "Media operation identity unavailable")
		return
	}
	operation := "media-process-" + hex.EncodeToString(nonce[:])
	claim, err := control.AdmitMediaPatch(&mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: operation, MediaId: id, Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_PROCESS, Process: options})
	if err != nil {
		mediaUnknown(w, operation)
		return
	}
	outcome, err := client.ExecuteMediaProcess(ctx, &mediav1.ExecuteMediaProcessRequest{SchemaVersion: 1, OperationId: operation, Fence: claim.Fence})
	if err == nil && outcome != nil {
		if _, err = control.RecordMediaPatchOutcome(outcome); err == nil {
			writeMediaProcessed(w, r, client, outcome)
			return
		}
	}
	queryCtx, queryCancel := context.WithTimeout(context.WithoutCancel(r.Context()), 5*time.Second)
	defer queryCancel()
	if retained, settled := recoverMediaPatch(queryCtx, control, client, claim); settled && retained != nil {
		writeMediaProcessed(w, r, client, retained)
		return
	}
	mediaUnknown(w, operation)
}
