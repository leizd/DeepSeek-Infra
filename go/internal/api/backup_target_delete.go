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
	if request.Method != http.MethodDelete || targetID == "" || strings.Contains(targetID, "/") {
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
	if _, err := control.DeleteBackupTarget(newOperationID(), newActionID(), "operator:public-api", targetID); err != nil {
		switch {
		case errors.Is(err, store.ErrBackupTargetNotFound):
			backupPolicyError(writer, http.StatusNotFound, "not_found", "Backup target not found")
		case errors.Is(err, store.ErrCutoverNotAuthorized), errors.Is(err, store.ErrDomainNotAuthoritative):
			backupPolicyError(writer, http.StatusServiceUnavailable, "GO_CONTROL_NOT_AUTHORITATIVE",
				"Backup targets have not completed native ownership cutover")
		case errors.Is(err, store.ErrWriterFenceHeld), errors.Is(err, store.ErrSchemaInactive):
			backupPolicyError(writer, http.StatusServiceUnavailable, "GO_CONTROL_UNAVAILABLE", "Go control store is not writable right now")
		default:
			backupPolicyError(writer, http.StatusInternalServerError, "GO_CONTROL_WRITE_FAILED", "Backup target deletion failed")
		}
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(map[string]any{"deleted": true, "targetId": targetID})
}
