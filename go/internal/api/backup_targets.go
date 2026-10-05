package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func backupTargetsList(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodGet {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if !allowedPublicHost(request) {
		backupPolicyError(writer, http.StatusForbidden, "forbidden", "Host not allowed")
		return
	}
	if !authorizedPublicRequest(request) {
		backupPolicyError(writer, http.StatusUnauthorized, "unauthorized", "Auth required")
		return
	}
	if control == nil {
		backupPolicyError(writer, http.StatusServiceUnavailable, "CONTROL_UNAVAILABLE", "Go control store unavailable")
		return
	}
	records, health, err := control.ListAuthoritativeTargets()
	if err != nil {
		switch {
		case errors.Is(err, store.ErrDomainNotAuthoritative):
			backupPolicyError(writer, http.StatusServiceUnavailable, "GO_CONTROL_NOT_AUTHORITATIVE", "Backup targets have not completed native ownership cutover")
		case errors.Is(err, store.ErrTargetHealthNotTransferred):
			backupPolicyError(writer, http.StatusServiceUnavailable, "TARGET_HEALTH_NOT_TRANSFERRED", "Backup target health has not completed native transfer")
		default:
			backupPolicyError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup target store read failed")
		}
		return
	}
	targets := make([]map[string]any, 0, len(records))
	for _, record := range records {
		if record.State == "DELETED" {
			continue
		}
		var target map[string]any
		decoder := json.NewDecoder(strings.NewReader(string(record.Payload)))
		decoder.UseNumber()
		if err := decoder.Decode(&target); err != nil || target == nil || target["targetId"] != record.ID {
			backupPolicyError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup target payload invalid")
			return
		}
		targets = append(targets, target)
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(map[string]any{"targets": targets, "health": health})
}
