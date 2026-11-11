package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func targetDeleteContractCall(t *testing.T, mux *http.ServeMux, identifier, host, token string) (int, map[string]any) {
	t.Helper()
	request := httptest.NewRequest(http.MethodDelete, "/api/workspace/backup-targets/"+identifier, nil)
	request.Host = host
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	var body map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
		t.Fatalf("delete did not return a public JSON envelope: %v", err)
	}
	return response.Code, body
}

func TestTargetDeleteContractRequiresOriginalSuccessAndJournalledAbsence(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	control := targetHealthPublicStore(t, false, 1000)
	records, health, err := control.ListAuthoritativeTargets()
	if err != nil || len(records) != 1 {
		t.Fatal("signed imported target fixture did not become authoritative", err)
	}
	before := records[0]
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	status, body := targetDeleteContractCall(t, mux, before.ID, "127.0.0.1", "target-public-secret")
	if status != http.StatusOK || body["deleted"] != true || body["targetId"] != before.ID || len(body) != 2 {
		t.Fatalf("original legal delete contract: status=%d body=%v", status, body)
	}
	retained, exists, err := control.Get("target", before.ID)
	if err != nil || !exists || retained.State != store.TombstoneState || retained.Revision != before.Revision+1 {
		t.Fatalf("deleted target lost immutable journal continuity: exists=%v row=%+v err=%v", exists, retained, err)
	}
	_, afterHealth, err := control.ListAuthoritativeTargets()
	if err != nil || !reflect.DeepEqual(health, afterHealth) {
		t.Fatal("deletion changed unrelated imported health history", err)
	}
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, targetListRequest())
	var listed struct {
		Targets []map[string]any `json:"targets"`
	}
	if response.Code != http.StatusOK || json.Unmarshal(response.Body.Bytes(), &listed) != nil {
		t.Fatal("target list failed after deletion", response.Code)
	}
	for _, record := range listed.Targets {
		if record["targetId"] == before.ID {
			t.Fatal("deleted target remained publicly registered")
		}
	}
	status, body = targetDeleteContractCall(t, mux, before.ID, "127.0.0.1", "target-public-secret")
	if status != http.StatusNotFound || body["code"] != "not_found" || body["error"] != "Backup target not found" {
		t.Fatalf("repeated delete changed original absence semantics: status=%d body=%v", status, body)
	}
	stable, _, err := control.Get("target", before.ID)
	if err != nil || stable.Revision != retained.Revision {
		t.Fatal("an absent target delete appended another mutation", err)
	}
}

func TestTargetDeleteContractKeepsMissingTargetsAsNotFound(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	control := targetHealthPublicStore(t, true, 1000)
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	status, body := targetDeleteContractCall(t, mux, "missing-contract-target", "127.0.0.1", "target-public-secret")
	if status != http.StatusNotFound || body["code"] != "not_found" || body["error"] != "Backup target not found" {
		t.Fatalf("original missing-target contract: status=%d body=%v", status, body)
	}
}

func TestTargetDeleteContractRequiresAuthenticationHostAndNativeAuthority(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	for _, test := range []struct {
		host, token, code string
		status            int
	}{
		{"127.0.0.1", "", "unauthorized", http.StatusUnauthorized},
		{"127.0.0.1", "incorrect", "unauthorized", http.StatusUnauthorized},
		{"foreign.example", "target-public-secret", "forbidden", http.StatusForbidden},
		{"127.0.0.1", "target-public-secret", "CONTROL_UNAVAILABLE", http.StatusServiceUnavailable},
	} {
		mux := http.NewServeMux()
		RegisterPublic(mux, nil)
		status, body := targetDeleteContractCall(t, mux, "missing-contract-target", test.host, test.token)
		if status != test.status || body["code"] != test.code {
			t.Fatalf("public admission: status=%d body=%v want=%+v", status, body, test)
		}
	}
	shadow, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "target-delete-shadow-contract"})
	if err != nil {
		t.Fatal(err)
	}
	defer shadow.Close()
	mux := http.NewServeMux()
	RegisterPublic(mux, shadow)
	status, body := targetDeleteContractCall(t, mux, "missing-contract-target", "127.0.0.1", "target-public-secret")
	if status != http.StatusServiceUnavailable || body["code"] != "GO_CONTROL_NOT_AUTHORITATIVE" {
		t.Fatalf("unpromoted target domain admitted delete: status=%d body=%v", status, body)
	}
}
