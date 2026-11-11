package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func targetHealthPublicStore(t *testing.T, empty bool, now int64) *store.Control {
	t.Helper()
	return targetPublicFixtureStore(t, empty, true, now)
}

func targetPublicFixtureStore(t *testing.T, empty, withHealth bool, now int64) *store.Control {
	t.Helper()
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "target-health-api",
		Now: func() int64 { return now }, AuthorizeCutover: true, PromotionSignerPublicKey: promotionRoutePublic,
		FleetID: mutationFleetID, Environment: mutationEnvironment})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	directory, prefix := "target-health-v1", "python_"
	if empty {
		directory, prefix = "target-health-empty-v1", "python_empty_"
	}
	if !withHealth {
		directory = ""
	}
	read := func(name string) []byte {
		raw, err := os.ReadFile(filepath.Join("..", "store", "testdata", directory, prefix+name))
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	var checkpoint store.AuthorityCheckpoint
	if err := json.Unmarshal(read("inventory_checkpoint_v1.json"), &checkpoint); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.ClaimControlAuthority(&checkpoint); err != nil {
		t.Fatal(err)
	}
	current, err := control.GetCutover("target")
	if err != nil {
		t.Fatal(err)
	}
	dual, err := control.TransitionCutover(store.CutoverTransition{Domain: "target", To: store.CutoverDualEvaluate,
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
		TransferID: "target-health-dual"})
	if err != nil {
		t.Fatal(err)
	}
	source := copyPythonSourceFixture(t, filepath.Join(directory, prefix+"control_source_v1.sqlite3"), "target")
	exportName := "target_inventory_export_v1.json"
	if withHealth {
		schedulerDir := filepath.Join(filepath.Dir(filepath.Dir(source)), ".backup-scheduler")
		if err := os.MkdirAll(schedulerDir, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(schedulerDir, "scheduler.db"), read("scheduler_source_v1.sqlite3"), 0o600); err != nil {
			t.Fatal(err)
		}
		exportName = "target_inventory_export_v2.json"
	}
	raw := read(exportName)
	attested, err := store.AttestPythonInventorySource(source, raw)
	if err != nil {
		t.Fatal(err)
	}
	imported, err := control.ImportAttestedPythonInventory(raw, attested)
	if err != nil {
		t.Fatal(err)
	}
	request := store.CutoverTransition{Domain: "target", To: store.CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: imported.TransferID, Authority: &checkpoint}
	if _, err := control.TransitionCutover(signedPromotionRoute(t, request, dual, now, &imported)); err != nil {
		t.Fatal(err)
	}
	return control
}

func TestBackupTargetsFailClosedAtThePublicReadBoundary(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	mux := http.NewServeMux()
	RegisterPublic(mux, nil)
	for _, check := range []struct {
		method, host, token string
		status              int
		code                string
	}{
		{http.MethodGet, "foreign.example", "", http.StatusForbidden, "forbidden"},
		{http.MethodGet, "127.0.0.1", "", http.StatusUnauthorized, "unauthorized"},
		{http.MethodGet, "127.0.0.1", "wrong", http.StatusUnauthorized, "unauthorized"},
		{http.MethodGet, "127.0.0.1", "target-public-secret", http.StatusServiceUnavailable, "CONTROL_UNAVAILABLE"},
		{http.MethodPost, "127.0.0.1", "target-public-secret", http.StatusMethodNotAllowed, ""},
	} {
		request := httptest.NewRequest(check.method, "/api/workspace/backup-targets", nil)
		request.Host = check.host
		if check.token != "" {
			request.Header.Set("Authorization", "Bearer "+check.token)
		}
		response := httptest.NewRecorder()
		mux.ServeHTTP(response, request)
		if response.Code != check.status || check.code != "" && !bytes.Contains(response.Body.Bytes(), []byte(check.code)) {
			t.Fatalf("public target admission %+v: %d %s", check, response.Code, response.Body.String())
		}
	}
	shadow, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "target-api-shadow"})
	if err != nil {
		t.Fatal(err)
	}
	defer shadow.Close()
	mux = http.NewServeMux()
	RegisterPublic(mux, shadow)
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, targetListRequest())
	if response.Code != http.StatusServiceUnavailable || !bytes.Contains(response.Body.Bytes(), []byte("GO_CONTROL_NOT_AUTHORITATIVE")) {
		t.Fatalf("shadow targets appeared usable: %d %s", response.Code, response.Body.String())
	}
	if err := shadow.Close(); err != nil {
		t.Fatal(err)
	}
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, targetListRequest())
	if response.Code != http.StatusInternalServerError || !bytes.Contains(response.Body.Bytes(), []byte("GO_CONTROL_READ_FAILED")) {
		t.Fatalf("closed control store appeared empty: %d %s", response.Code, response.Body.String())
	}
}

func TestBackupTargetsCannotInventEmptyHealthForAnOlderImport(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	control := targetPublicFixtureStore(t, false, false, 1000)
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, targetListRequest())
	if response.Code != http.StatusServiceUnavailable || !bytes.Contains(response.Body.Bytes(), []byte("TARGET_HEALTH_NOT_TRANSFERRED")) {
		t.Fatalf("v1 target import invented an empty health source: %d %s", response.Code, response.Body.String())
	}
}

func targetListRequest() *http.Request {
	request := httptest.NewRequest(http.MethodGet, "/api/workspace/backup-targets", nil)
	request.Host = "127.0.0.1"
	request.Header.Set("Authorization", "Bearer target-public-secret")
	return request
}

func TestBackupTargetsPublicRoutePreservesImportedHealthIncludingEmptySource(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	for _, empty := range []bool{false, true} {
		control := targetHealthPublicStore(t, empty, 1000)
		mux := http.NewServeMux()
		RegisterPublic(mux, control)
		response := httptest.NewRecorder()
		mux.ServeHTTP(response, targetListRequest())
		var listed struct {
			Targets []map[string]any     `json:"targets"`
			Health  []store.TargetHealth `json:"health"`
		}
		if response.Code != http.StatusOK || json.Unmarshal(response.Body.Bytes(), &listed) != nil {
			t.Fatalf("native target list: %d %s", response.Code, response.Body.String())
		}
		if empty {
			if listed.Targets == nil || listed.Health == nil || len(listed.Targets) != 0 || len(listed.Health) != 0 {
				t.Fatalf("attested empty response must use arrays: %s", response.Body.String())
			}
		} else if len(listed.Targets) != 1 || listed.Targets[0]["targetId"] != "t-1" || len(listed.Health) != 2 ||
			listed.Health[0].TargetID != "old-target" || listed.Health[0].Detail != nil ||
			listed.Health[1].Status != "blocked" || listed.Health[1].Detail == nil || *listed.Health[1].Detail != "provider timeout" {
			t.Fatalf("target or health history lost: %s", response.Body.String())
		}
	}
}
