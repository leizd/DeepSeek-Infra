//go:build native_integration

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

// The target health originates in an isolated copy of the real Python scheduler
// schema, is fenced and imported with the target inventory, and is then read
// only from Go through two separately built native processes. Python is not
// launched by this test or either service.
func TestBackupTargetRustGatewayToGoProcess(t *testing.T) {
	browserHoldSeconds := nativeBackupBrowserHold(t)
	control := targetHealthPublicStore(t, false, 1000)
	seedProcessPolicyForTarget(t, control)
	storeDir := filepath.Dir(control.DatabasePath())
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	processes := startNativeProcesses(t, storeDir, browserHoldSeconds, false)
	status, body := processes.request(t, http.MethodGet, "/api/workspace/backup-targets", policyProcessHost, policyProcessSecret, nil)
	if status != http.StatusOK {
		t.Fatalf("Rust -> Go target read failed: status=%d body=%s", status, body)
	}
	var listed struct {
		Targets []map[string]any     `json:"targets"`
		Health  []store.TargetHealth `json:"health"`
	}
	if json.Unmarshal(body, &listed) != nil || len(listed.Targets) != 1 ||
		listed.Targets[0]["targetId"] != "t-1" || len(listed.Health) != 2 ||
		listed.Health[1].Status != "blocked" || listed.Health[1].Detail == nil ||
		*listed.Health[1].Detail != "provider timeout" {
		t.Fatalf("Rust -> Go target health projection failed: %s", body)
	}
	if processes.hold(t) {
		return
	}
	processes.assertReadAdmissionRefusals(t, "/api/workspace/backup-targets")
}

// The automatic-backup page loads policy, target and mirror inventories. Give
// the policy view its own real import and signed Go mutation so the target view
// can be observed without replacing either native response with a UI fixture.
func seedProcessPolicyForTarget(t *testing.T, control *store.Control) {
	t.Helper()
	directory := filepath.Join("..", "store", "testdata", "target-health-v1")
	raw, err := os.ReadFile(filepath.Join(directory, "python_inventory_checkpoint_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var checkpoint store.AuthorityCheckpoint
	if err := json.Unmarshal(raw, &checkpoint); err != nil {
		t.Fatal(err)
	}
	current, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	dual, err := control.TransitionCutover(store.CutoverTransition{
		Domain: "policy", To: store.CutoverDualEvaluate, TransferID: "process-policy-dual",
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
	})
	if err != nil {
		t.Fatal(err)
	}
	raw, err = os.ReadFile(filepath.Join(directory, "python_policy_inventory_export_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	source := copyPythonSourceFixture(t, filepath.Join("target-health-v1", "python_control_source_v1.sqlite3"), "policy")
	attested, err := store.AttestPythonInventorySource(source, raw)
	if err != nil {
		t.Fatal(err)
	}
	imported, err := control.ImportAttestedPythonInventory(raw, attested)
	if err != nil {
		t.Fatal(err)
	}
	request := store.CutoverTransition{Domain: "policy", To: store.CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: imported.TransferID, Authority: &checkpoint}
	if _, err := control.TransitionCutover(signedPromotionRoute(t, request, dual, 1000, &imported)); err != nil {
		t.Fatal(err)
	}
	private, public := applySigner(t)
	raw = signedApplyRequest(t, control, private, public, repeatTestHex("e"), "p-1", "DISABLED", 3,
		map[string]any{
			"schemaVersion": 1, "enabled": false, "name": "native target health",
			"schedule":       map[string]any{"cron": "0 3 * * *", "timezone": "UTC", "misfirePolicy": "skip", "catchupWindowSeconds": 86400, "jitterSeconds": 0},
			"scope":          map[string]any{"mode": "full", "projectIds": []string{}, "includeHistory": true, "includeExternalState": true, "coveragePolicy": "strict"},
			"frontendMirror": map[string]any{"mode": "best-effort", "maxAgeSeconds": 3600},
			"protection":     map[string]any{"mode": "age-recipient", "recipients": []string{"age1testfixture"}},
			"targetId":       "t-1", "retentionPolicyId": "default",
			"retry":     map[string]any{"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 3600},
			"createdAt": "2026-09-05T00:00:00Z", "updatedAt": "2026-09-05T00:00:00Z",
		})
	mux := http.NewServeMux()
	RegisterWithOptions(mux, control, applyOptions(public))
	applyRequest := httptest.NewRequest(http.MethodPost, "/internal/mutation/apply", bytes.NewReader(raw))
	applyRequest.RemoteAddr = "127.0.0.1:1"
	applyRequest.Header.Set("Authorization", "Bearer "+testInternalBearer)
	applyRequest.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, applyRequest)
	if response.Code != http.StatusOK {
		t.Fatalf("signed policy write for browser observation: %d %s", response.Code, response.Body.String())
	}
}
