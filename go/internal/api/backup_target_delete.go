package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func backupTargetResource(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	targetID := strings.TrimPrefix(request.URL.Path, "/api/workspace/backup-targets/")
	activate := request.Method == http.MethodPost && strings.HasSuffix(targetID, "/activate")
	if activate {
		targetID = strings.TrimSuffix(targetID, "/activate")
	}
	if (!activate && request.Method != http.MethodDelete) || targetID == "" || strings.Contains(targetID, "/") {
		notImplemented(writer, request)
		return
	}
	if !authorizationAndHost(writer, request) {
		return
	}
	if control == nil {
		backupPolicyError(writer, http.StatusServiceUnavailable, "CONTROL_UNAVAILABLE", "Go control store unavailable")
		return
	}
	if activate {
		// The original route ignores its body, including expectedGeneration.
		record, _, err := control.ActivateBackupTarget(newOperationID(), newActionID(), "operator:public-api", targetID)
		if err != nil {
			backupTargetMutationRefusal(writer, err, "activation")
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		public, err := store.PublicBackupTargetRecord(record.Payload)
		if err != nil {
			backupTargetMutationRefusal(writer, err, "activation")
			return
		}
		_, _ = writer.Write(public)
		return
	}
	if _, err := control.DeleteBackupTarget(newOperationID(), newActionID(), "operator:public-api", targetID); err != nil {
		backupTargetMutationRefusal(writer, err, "deletion")
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(map[string]any{"deleted": true, "targetId": targetID})
}

func backupTargetMutationRefusal(writer http.ResponseWriter, err error, operation string) {
	switch {
	case errors.Is(err, store.ErrBackupTargetNotFound):
		backupPolicyError(writer, http.StatusNotFound, "not_found", "Backup target not found")
	case errors.Is(err, store.ErrCutoverNotAuthorized), errors.Is(err, store.ErrDomainNotAuthoritative):
		backupPolicyError(writer, http.StatusServiceUnavailable, "GO_CONTROL_NOT_AUTHORITATIVE",
			"Backup targets have not completed native ownership cutover")
	case errors.Is(err, store.ErrWriterFenceHeld), errors.Is(err, store.ErrSchemaInactive):
		backupPolicyError(writer, http.StatusServiceUnavailable, "GO_CONTROL_UNAVAILABLE", "Go control store is not writable right now")
	default:
		backupPolicyError(writer, http.StatusInternalServerError, "GO_CONTROL_WRITE_FAILED", "Backup target "+operation+" failed")
	}
}
