package api

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/protobuf/proto"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

func writeMediaCreated(w http.ResponseWriter, r *http.Request, client *worker.Client, outcome *mediav1.MediaPatchOutcome) {
	if value, ok := mediaCreatedRecord(w, r, client, outcome); ok {
		writeJSON(w, r, http.MethodPost, map[string]any{"ok": true, "media": value})
	}
}

func mediaCreatedRecord(w http.ResponseWriter, r *http.Request, client *worker.Client, outcome *mediav1.MediaPatchOutcome) (any, bool) {
	w.Header().Set("X-DeepSeek-Operation-Id", outcome.OperationId)
	if outcome.State == mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED {
		policyWriteError(w, int(outcome.HttpStatus), outcome.ErrorCode, outcome.ErrorMessage)
		return nil, false
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), 10*time.Second)
	defer cancel()
	record, err := client.ReadMediaCreateResult(ctx, outcome)
	if err != nil {
		mediaUnknown(w, outcome.OperationId)
		return nil, false
	}
	value, err := mediamodel.ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: record}})
	if err != nil {
		mediaUnknown(w, outcome.OperationId)
		return nil, false
	}
	return value, true
}

func mediaCreate(w http.ResponseWriter, r *http.Request, control *store.Control, view PublicView) {
	if r.Method != http.MethodPost {
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
	// Retain the original case-sensitive multipart route selection.
	if strings.Contains(r.Header.Get("Content-Type"), "multipart/form-data") {
		mediaUpload(w, r, control)
		return
	}
	if r.ContentLength == 0 {
		policyWriteError(w, 400, "invalid_payload", "Request body is empty")
		return
	}
	if r.ContentLength > 16_000_000 {
		policyWriteError(w, 413, "upload_too_large", "Request body is too large")
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 16_000_000))
	if err != nil {
		policyWriteError(w, 413, "upload_too_large", "Request body is too large")
		return
	}
	value, err := mediamodel.ParseJSONBounded(raw, 16_000_000)
	if err != nil || value.GetObjectValue() == nil || len(bytes.TrimSpace(raw)) == 0 {
		policyWriteError(w, 400, "invalid_payload", "Request body must be a JSON object")
		return
	}
	payload, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&mediav1.MediaCreatePayload{Registration: value.GetObjectValue()})
	if err != nil || len(payload) == 0 || len(payload) > mediamodel.MaxCreatePayloadBytes {
		policyWriteError(w, 400, "invalid_payload", "Invalid media registration")
		return
	}
	client, err := dialMediaWorker()
	if err != nil {
		policyWriteError(w, 503, "MEDIA_WORKER_UNAVAILABLE", "Native media worker unavailable")
		return
	}
	defer client.Close()
	if outcome, ok := performMediaCreate(w, r, control, client, payload); ok {
		writeMediaCreated(w, r, client, outcome)
	}
}

func performMediaCreate(w http.ResponseWriter, r *http.Request, control *store.Control, client *worker.Client, payload []byte) (*mediav1.MediaPatchOutcome, bool) {
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	pending, err := control.PendingMediaPatch()
	if err != nil {
		mediaUnknown(w, "")
		return nil, false
	}
	if pending != nil {
		if _, settled := recoverMediaPatch(ctx, control, client, pending); !settled {
			mediaUnknown(w, pending.Intent.OperationId)
			return nil, false
		}
	}
	var nonce [16]byte
	if _, err = rand.Read(nonce[:]); err != nil {
		policyWriteError(w, 503, "MEDIA_ID_UNAVAILABLE", "Media operation identity unavailable")
		return nil, false
	}
	operation := "media-create-" + hex.EncodeToString(nonce[:])
	var id [8]byte
	if _, err = rand.Read(id[:]); err != nil {
		policyWriteError(w, 503, "MEDIA_ID_UNAVAILABLE", "Media identity unavailable")
		return nil, false
	}
	digest := sha256.Sum256(payload)
	intent := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: operation, MediaId: "media_" + hex.EncodeToString(id[:]),
		Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_CREATE, Create: &mediav1.MediaCreateManifest{PayloadSha256: digest[:], PayloadBytes: uint64(len(payload))}}
	claim, err := control.AdmitMediaPatch(intent)
	if err != nil {
		mediaUnknown(w, operation)
		return nil, false
	}
	if err = client.StageMediaCreate(ctx, claim, payload); err == nil {
		var outcome *mediav1.MediaPatchOutcome
		outcome, err = client.ExecuteMediaCreate(ctx, &mediav1.ExecuteMediaPatchRequest{SchemaVersion: 1, OperationId: operation, Fence: claim.Fence})
		if err == nil && outcome != nil {
			if _, recordErr := control.RecordMediaPatchOutcome(outcome); recordErr == nil {
				return outcome, true
			}
		}
	}
	queryCtx, queryCancel := context.WithTimeout(context.WithoutCancel(r.Context()), 5*time.Second)
	defer queryCancel()
	if retained, settled := recoverMediaPatch(queryCtx, control, client, claim); settled && retained != nil {
		return retained, true
	}
	mediaUnknown(w, operation)
	return nil, false
}
