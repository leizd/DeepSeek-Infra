package api

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/scheduler"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func publicToken(request *http.Request) string {
	if value := strings.TrimSpace(request.Header.Get("Authorization")); len(value) >= 7 && strings.EqualFold(value[:7], "bearer ") {
		return strings.TrimSpace(value[7:])
	}
	if cookie, err := request.Cookie("auth_token"); err == nil {
		return cookie.Value
	}
	return ""
}

func authorizedPublicRequest(request *http.Request) bool {
	if envBool("AUTH_DISABLED", false) {
		return true
	}
	expected := strings.TrimSpace(os.Getenv("AUTH_TOKEN"))
	if expected == "" {
		return false
	}
	provided := publicToken(request)
	left, right := sha256.Sum256([]byte(provided)), sha256.Sum256([]byte(expected))
	return subtle.ConstantTimeCompare(left[:], right[:]) == 1
}

func publicHost(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	if strings.ContainsAny(value, "/\\") {
		return ""
	}
	if host, _, err := net.SplitHostPort(value); err == nil {
		return strings.Trim(host, "[]")
	}
	if strings.HasPrefix(value, "[") && strings.HasSuffix(value, "]") {
		return strings.Trim(value, "[]")
	}
	return value
}

func allowedPublicHost(request *http.Request) bool {
	if envBool("AUTH_DISABLED", false) {
		return true
	}
	allowed := map[string]bool{"localhost": true, "127.0.0.1": true, "::1": true}
	if configured := publicHost(os.Getenv("HOST")); configured != "" && configured != "0.0.0.0" {
		allowed[configured] = true
	}
	for _, configured := range strings.Split(os.Getenv("AUTH_ALLOWED_HOSTS"), ",") {
		if host := publicHost(configured); host != "" {
			allowed[host] = true
		}
	}
	if addresses, err := net.InterfaceAddrs(); err == nil {
		for _, address := range addresses {
			if ipnet, ok := address.(*net.IPNet); ok && ipnet.IP.IsGlobalUnicast() {
				allowed[strings.ToLower(ipnet.IP.String())] = true
			}
		}
	}
	host := publicHost(request.Host)
	return host != "" && allowed[host]
}

func backupPolicyError(writer http.ResponseWriter, code int, name, message string) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(code)
	_ = json.NewEncoder(writer).Encode(map[string]string{"error": message, "code": name})
}

// backupPoliciesList keeps the existing browser API response shape. The Go
// cutover state, record payloads, and event histories are read transactionally
// by ListAuthoritativeRecords; a shadow read never reports success.
func backupPoliciesList(writer http.ResponseWriter, request *http.Request, control *store.Control) {
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
	records, err := control.ListAuthoritativeRecords("policy")
	if err != nil {
		if errors.Is(err, store.ErrDomainNotAuthoritative) {
			backupPolicyError(writer, http.StatusServiceUnavailable, "GO_CONTROL_NOT_AUTHORITATIVE", "Backup policies have not completed native ownership cutover")
			return
		}
		backupPolicyError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup policy store read failed")
		return
	}
	policies := make([]map[string]any, 0, len(records))
	nextRuns := make(map[string]*scheduler.BackupNextRun, len(records))
	now := time.Now().UTC()
	for _, record := range records {
		var policy map[string]any
		decoder := json.NewDecoder(strings.NewReader(string(record.Payload)))
		decoder.UseNumber()
		if err := decoder.Decode(&policy); err != nil || policy == nil || backupPolicyID(policy) != record.ID {
			backupPolicyError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED", "Backup policy payload invalid")
			return
		}
		policies = append(policies, policy)
		nextRuns[record.ID] = scheduler.NextBackupRun(policy, now)
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(map[string]any{"policies": policies, "nextRuns": nextRuns})
}

func backupPolicyID(policy map[string]any) string {
	id, _ := policy["policyId"].(string)
	return id
}
