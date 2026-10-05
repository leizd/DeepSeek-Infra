//go:build native_integration

package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// This test seeds an isolated Go store through the signed-mutation handler,
// then restarts it in a real deepseekd process behind a real Rust gateway
// process. Set both *_TEST_BINARY variables to freshly built executables and
// run with -tags native_integration.
func TestBackupPolicyRustGatewayToGoProcess(t *testing.T) {
	browserHoldSeconds := nativeBackupBrowserHold(t)
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "native-process-policy-secret")
	control := applyStore(t)
	private, public := applySigner(t)
	mux := http.NewServeMux()
	RegisterWithOptions(mux, control, applyOptions(public))
	RegisterPublic(mux, control)
	goServer := httptest.NewServer(mux)
	defer goServer.Close()

	raw := signedApplyRequest(t, control, private, public, repeatTestHex("d"),
		"p-process-boundary", "ACTIVE", 1,
		map[string]any{
			"schemaVersion": 1, "enabled": false, "name": "native process boundary",
			"schedule":       map[string]any{"cron": "0 3 * * *", "timezone": "UTC", "misfirePolicy": "skip", "catchupWindowSeconds": 86400, "jitterSeconds": 0},
			"scope":          map[string]any{"mode": "full", "projectIds": []string{}, "includeHistory": true, "includeExternalState": true, "coveragePolicy": "strict"},
			"frontendMirror": map[string]any{"mode": "best-effort", "maxAgeSeconds": 3600},
			"protection":     map[string]any{"mode": "age-recipient", "recipients": []string{"age1testfixture"}},
			"targetId":       "managed-local", "retentionPolicyId": "default",
			"retry":     map[string]any{"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 3600},
			"createdAt": "2026-09-05T00:00:00Z", "updatedAt": "2026-09-05T00:00:00Z",
		})
	applyRequest, err := http.NewRequest(http.MethodPost, goServer.URL+"/internal/mutation/apply", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	applyRequest.Header.Set("Authorization", "Bearer "+testInternalBearer)
	applyRequest.Header.Set("Content-Type", "application/json")
	applyResponse, err := http.DefaultClient.Do(applyRequest)
	if err != nil {
		t.Fatal(err)
	}
	applyBody, err := io.ReadAll(applyResponse.Body)
	_ = applyResponse.Body.Close()
	if err != nil || applyResponse.StatusCode != http.StatusOK {
		t.Fatalf("Go signed mutation failed: status=%d body=%s error=%v", applyResponse.StatusCode, applyBody, err)
	}
	goStoreDir := filepath.Dir(control.DatabasePath())
	goServer.Close()
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	processes := startNativeProcesses(t, goStoreDir, browserHoldSeconds, false)
	status, body := processes.request(t, http.MethodGet, "/api/workspace/backup-policies", policyProcessHost, policyProcessSecret, nil)
	if status != http.StatusOK {
		t.Fatalf("Rust→Go public read failed: status=%d body=%s", status, body)
	}
	var listed struct {
		Policies []map[string]any           `json:"policies"`
		NextRuns map[string]json.RawMessage `json:"nextRuns"`
	}
	var nextRun struct {
		Timezone string `json:"timezone"`
	}
	if json.Unmarshal(body, &listed) != nil ||
		len(listed.Policies) != 1 || listed.Policies[0]["policyId"] != "p-process-boundary" ||
		json.Unmarshal(listed.NextRuns["p-process-boundary"], &nextRun) != nil || nextRun.Timezone != "UTC" {
		t.Fatalf("Rust→Go public policy projection failed: body=%s", body)
	}
	if processes.hold(t) {
		return
	}
	processes.assertReadAdmissionRefusals(t, "/api/workspace/backup-policies")
}

// internalBearerIfAuthorized is empty unless the daemon is allowed control authority,
// which config.Load refuses without an authenticated internal plane.
func internalBearerIfAuthorized(controlAuthority bool) string {
	if !controlAuthority {
		return ""
	}
	return policyProcessInternalBearer
}

func nativeBackupBrowserHold(t *testing.T) int {
	t.Helper()
	browserHoldSeconds := 0
	if raw := os.Getenv("DEEPSEEK_NATIVE_BROWSER_HOLD_SECONDS"); raw != "" {
		var parseErr error
		browserHoldSeconds, parseErr = strconv.Atoi(raw)
		if parseErr != nil || browserHoldSeconds < 1 || browserHoldSeconds > 300 {
			t.Fatalf("invalid browser hold seconds %q: require 1..300", raw)
		}
	}
	return browserHoldSeconds
}

const (
	policyProcessHost = "localhost:8787"
	// policyProcessSecret is the public browser token; the internal bearer is the
	// separate credential that makes a deployment allowed to serve control writes.
	policyProcessSecret         = "native-process-policy-secret"
	policyProcessInternalBearer = "native-process-internal-bearer-0000000000"
)

// nativeProcesses owns one real deepseekd and one real Rust gateway in front of it,
// both bound to the same isolated Go store directory.
type nativeProcesses struct {
	baseURL     string
	holdSeconds int
	client      *http.Client
	gatewayLog  func() string
	daemonLog   func() string
	stopGateway func()
	stopDaemon  func()
}

// syncBuffer is a bytes.Buffer that exec's writer goroutines and the test goroutine
// can both touch.
type syncBuffer struct {
	mu   sync.Mutex
	data bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.data.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.data.String()
}

// request drives the public API through the real Rust edge. A nil body sends no body.
func (p *nativeProcesses) request(t *testing.T, method, path, host, token string, body []byte) (int, []byte) {
	t.Helper()
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, p.baseURL+path, reader)
	if err != nil {
		t.Fatal(err)
	}
	req.Host = host
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := p.client.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v\ngateway=%s\ndaemon=%s", method, path, err, p.gatewayLog(), p.daemonLog())
	}
	payload, err := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if err != nil {
		t.Fatalf("%s %s: reading the body: %v\ngateway=%s\ndaemon=%s", method, path, err, p.gatewayLog(), p.daemonLog())
	}
	return response.StatusCode, payload
}

// hold keeps both processes alive for genuine browser observation and reports whether
// it held. The browser needs no credential because the hold run disables token auth.
func (p *nativeProcesses) hold(t *testing.T) bool {
	t.Helper()
	if p.holdSeconds == 0 {
		return false
	}
	t.Logf("BROWSER_URL=%s/", p.baseURL)
	t.Logf("BROWSER_API=%s", p.baseURL)
	time.Sleep(time.Duration(p.holdSeconds) * time.Second)
	return true
}

func (p *nativeProcesses) assertReadAdmissionRefusals(t *testing.T, apiPath string) {
	t.Helper()
	for _, check := range []struct {
		host, token string
		status      int
	}{
		{"foreign.example", policyProcessSecret, http.StatusForbidden},
		{"foreign.example", "", http.StatusForbidden},
		{policyProcessHost, "", http.StatusUnauthorized},
	} {
		status, body := p.request(t, http.MethodGet, apiPath, check.host, check.token, nil)
		if status != check.status {
			t.Fatalf("Rust edge read admission host=%q token=%v: status=%d body=%s",
				check.host, check.token != "", status, body)
		}
	}
}

func (p *nativeProcesses) assertWriteAdmissionRefusals(t *testing.T, collection string) {
	t.Helper()
	for _, check := range []struct {
		name, method, path, host, token string
		status                          int
	}{
		{"create foreign host", http.MethodPost, collection, "foreign.example", policyProcessSecret, http.StatusForbidden},
		{"create without token", http.MethodPost, collection, policyProcessHost, "", http.StatusUnauthorized},
		{"update foreign host", http.MethodPatch, collection + "/p-crud-admission", "foreign.example", policyProcessSecret, http.StatusForbidden},
		{"delete without token", http.MethodDelete, collection + "/p-crud-admission", policyProcessHost, "", http.StatusUnauthorized},
	} {
		var body []byte
		if check.method != http.MethodDelete {
			body = []byte(`{"name":"admission probe"}`)
		}
		status, payload := p.request(t, check.method, check.path, check.host, check.token, body)
		if status != check.status {
			t.Fatalf("Rust edge %s must be %d: status=%d body=%s", check.name, check.status, status, payload)
		}
	}
}

func startNativeProcesses(t *testing.T, goStoreDir string, browserHoldSeconds int, controlAuthority bool) *nativeProcesses {
	t.Helper()
	return startNativeProcessesWithWorkspace(t, goStoreDir, browserHoldSeconds, controlAuthority, t.TempDir())
}

func startNativeProcessesWithWorkspace(t *testing.T, goStoreDir string, browserHoldSeconds int, controlAuthority bool, workspaceRoot string) *nativeProcesses {
	t.Helper()
	authDisabled := "false"
	if browserHoldSeconds > 0 {
		authDisabled = "true"
	}
	binary := os.Getenv("DEEPSEEK_GATEWAY_TEST_BINARY")
	goBinary := os.Getenv("DEEPSEEKD_TEST_BINARY")
	if binary == "" {
		t.Fatal("DEEPSEEK_GATEWAY_TEST_BINARY must name the built Rust gateway")
	}
	if goBinary == "" {
		t.Fatal("DEEPSEEKD_TEST_BINARY must name the built Go daemon")
	}
	if !filepath.IsAbs(binary) {
		t.Fatalf("gateway test binary must have an absolute path: %q", binary)
	}
	if !filepath.IsAbs(goBinary) {
		t.Fatalf("Go daemon test binary must have an absolute path: %q", goBinary)
	}
	goReservation, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	goAddress := goReservation.Addr().String()
	_ = goReservation.Close()
	goCtx, goCancel := context.WithCancel(context.Background())
	// The cancel is owned by the Cleanup below, not by a defer: the two processes must
	// outlive this function, because the caller drives the flow through them.
	goCommand := exec.CommandContext(goCtx, goBinary)
	goCommand.Env = append(os.Environ(),
		"DEEPSEEKD_MODE=shadow",
		"DEEPSEEKD_LISTEN="+goAddress,
		"DEEPSEEKD_SHADOW_STORE="+goStoreDir,
		"DEEPSEEKD_PRODUCTION_STORE=",
		"DEEPSEEKD_OWNER=backup-policy-process-test",
		"DEEPSEEKD_CONTROL_AUTHORITY="+strconv.FormatBool(controlAuthority),
		"DEEPSEEKD_INTERNAL_BEARER="+internalBearerIfAuthorized(controlAuthority),
		"AUTH_DISABLED="+authDisabled,
		"AUTH_TOKEN=native-process-policy-secret",
	)
	var goOutput syncBuffer
	goCommand.Stdout, goCommand.Stderr = &goOutput, &goOutput
	if err := goCommand.Start(); err != nil {
		t.Fatal(err)
	}
	var goStopOnce sync.Once
	stopGo := func() {
		goStopOnce.Do(func() {
			goCancel()
			_ = goCommand.Wait()
		})
	}
	t.Cleanup(stopGo)
	client := &http.Client{Timeout: time.Second}
	goHealth := "http://" + goAddress + "/healthz"
	goDeadline := time.Now().Add(15 * time.Second)
	goReady := false
	for time.Now().Before(goDeadline) {
		response, err := client.Get(goHealth)
		if err == nil {
			_ = response.Body.Close()
			goReady = response.StatusCode == http.StatusOK
			if goReady {
				break
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	if !goReady {
		stopGo()
		t.Fatalf("deepseekd did not start: output=%s", goOutput.String())
	}

	staticRoot := t.TempDir()
	if browserHoldSeconds > 0 {
		staticRoot, err = filepath.Abs(filepath.Join("..", "..", "..", "static"))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := os.Stat(filepath.Join(staticRoot, "ui", "index.html")); err != nil {
			t.Fatalf("build the frontend before browser observation: %v", err)
		}
	} else {
		if err := os.Mkdir(filepath.Join(staticRoot, "ui"), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(staticRoot, "ui", "index.html"), []byte("native UI"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	reservation, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	gatewayAddress := reservation.Addr().String()
	_ = reservation.Close()
	if bind := os.Getenv("DEEPSEEK_NATIVE_BROWSER_BIND"); browserHoldSeconds > 0 && bind != "" {
		host, port, err := net.SplitHostPort(bind)
		if err != nil || (host != "0.0.0.0" && host != "127.0.0.1") || port == "" {
			t.Fatalf("invalid isolated browser bind: %q", bind)
		}
		gatewayAddress = bind
	}
	ctx, cancel := context.WithCancel(context.Background())
	// Same as the daemon above: the gateway's cancel is owned by the Cleanup, so it
	// stays up for the caller's whole flow.
	cmd := exec.CommandContext(ctx, binary)
	cmd.Env = append(os.Environ(),
		"GATEWAY_BIND_ADDR="+gatewayAddress,
		"GO_CONTROL_ADDR=http://"+goAddress,
		"DEEPSEEK_INTERNAL_BEARER="+internalBearerIfAuthorized(controlAuthority),
		"AUTH_DISABLED="+authDisabled,
		"AUTH_TOKEN=native-process-policy-secret",
		"AUTH_ALLOWED_HOSTS=",
		"HOST=127.0.0.1",
		"DEEPSEEK_INFRA_STATIC_DIR="+staticRoot,
		"DEEPSEEK_INFRA_ROOT="+workspaceRoot,
		"DEEPSEEK_BACKUP_MIRROR_DIR="+filepath.Join(workspaceRoot, ".backup-mirror"),
	)
	var processOutput syncBuffer
	cmd.Stdout, cmd.Stderr = &processOutput, &processOutput
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	var stopOnce sync.Once
	stop := func() {
		stopOnce.Do(func() {
			cancel()
			_ = cmd.Wait()
		})
	}
	t.Cleanup(stop)
	gatewayBase := "http://" + strings.Replace(gatewayAddress, "0.0.0.0:", "127.0.0.1:", 1)
	probeClient := &http.Client{Timeout: time.Second}
	deadline := time.Now().Add(15 * time.Second)
	var response *http.Response
	var body []byte
	for time.Now().Before(deadline) {
		var probe *http.Request
		probe, err = http.NewRequest(http.MethodGet, gatewayBase+"/api/workspace/backup-policies", nil)
		if err != nil {
			t.Fatal(err)
		}
		probe.Host = policyProcessHost
		probe.Header.Set("Authorization", "Bearer "+policyProcessSecret)
		response, err = probeClient.Do(probe)
		if err == nil {
			body, err = io.ReadAll(response.Body)
			_ = response.Body.Close()
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if err != nil {
		stop()
		t.Fatalf("Rust gateway did not start: %v; output=%s", err, processOutput.String())
	}
	if response.StatusCode != http.StatusOK {
		t.Fatalf("Rust→Go public read failed: status=%d body=%s", response.StatusCode, body)
	}
	return &nativeProcesses{
		baseURL:     gatewayBase,
		holdSeconds: browserHoldSeconds,
		client:      &http.Client{Timeout: 5 * time.Second},
		gatewayLog:  processOutput.String,
		daemonLog:   goOutput.String,
		stopGateway: stop,
		stopDaemon:  stopGo,
	}
}

// TestBackupPolicyRustGatewayToGoPolicyCrud drives the whole public policy CRUD over a
// real Rust gateway in front of a real deepseekd process: the create, the projected
// read, the merge-and-advance update, the tombstoning delete and the terminal 404 that
// follows it, plus the Host/token refusals on the write verbs. The store is promoted to
// Go-authoritative policy ownership by applyStore, which is the only state a write is
// admitted in. Set DEEPSEEK_NATIVE_BROWSER_HOLD_SECONDS to keep both processes up and
// leave one policy behind so the same flow can be observed in a browser.
func TestBackupPolicyRustGatewayToGoPolicyCrud(t *testing.T) {
	browserHoldSeconds := nativeBackupBrowserHold(t)
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", policyProcessSecret)
	control := applyStore(t)
	goStoreDir := filepath.Dir(control.DatabasePath())
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	processes := startNativeProcesses(t, goStoreDir, browserHoldSeconds, true)
	const collection = "/api/workspace/backup-policies"
	token := policyProcessSecret
	if browserHoldSeconds > 0 {
		// The hold run exists for a browser without a credential.
		token = ""
	}

	// A create through the edge must return the oracle's normalised document.
	status, body := processes.request(t, http.MethodPost, collection, policyProcessHost, token,
		[]byte(`{"policyId":"p-crud-boundary","name":"native crud boundary","protection":{"mode":"age-recipient","recipients":["age1fu59d59ghmr8x2t5dyzjs9xdcjgnakujp7mjy7cz2v7fq6vjqypskh4e62"]}}`))
	if status != http.StatusOK {
		t.Fatalf("policy create over Rust→Go: status=%d body=%s", status, body)
	}
	var created map[string]any
	if err := json.Unmarshal(body, &created); err != nil {
		t.Fatal(err)
	}
	if created["policyId"] != "p-crud-boundary" || created["policyRevision"] != float64(1) ||
		created["name"] != "native crud boundary" || created["primaryTargetId"] != "managed-local" {
		t.Fatalf("created document: %s", body)
	}
	createdAt, _ := created["createdAt"].(string)
	if createdAt == "" {
		t.Fatalf("created document has no createdAt: %s", body)
	}

	// The projected read must show it with its next run.
	listed := listProcessPolicies(t, processes, collection, token)
	if len(listed) != 1 || listed[0]["policyId"] != "p-crud-boundary" {
		t.Fatalf("created policy is not projected: %+v", listed)
	}

	// An update merges only the patch fields, advances the store-owned revision and
	// preserves the creation clock.
	status, body = processes.request(t, http.MethodPatch, collection+"/p-crud-boundary", policyProcessHost, token,
		[]byte(`{"name":"renamed boundary","enabled":true,"ignoredKey":"dropped"}`))
	if status != http.StatusOK {
		t.Fatalf("policy update over Rust→Go: status=%d body=%s", status, body)
	}
	var updated map[string]any
	if err := json.Unmarshal(body, &updated); err != nil {
		t.Fatal(err)
	}
	if updated["policyRevision"] != float64(2) || updated["name"] != "renamed boundary" || updated["enabled"] != true {
		t.Fatalf("updated document: %s", body)
	}
	if updated["createdAt"] != createdAt {
		t.Fatalf("update must preserve createdAt: %v -> %v", createdAt, updated["createdAt"])
	}
	if _, present := updated["ignoredKey"]; present {
		t.Fatalf("a field outside the patch list must not be stored: %s", body)
	}

	if processes.hold(t) {
		return
	}

	// A delete tombstones the record and answers with the oracle's document.
	status, body = processes.request(t, http.MethodDelete, collection+"/p-crud-boundary", policyProcessHost, token, nil)
	if status != http.StatusOK {
		t.Fatalf("policy delete over Rust→Go: status=%d body=%s", status, body)
	}
	var deleted map[string]any
	if err := json.Unmarshal(body, &deleted); err != nil {
		t.Fatal(err)
	}
	if deleted["deleted"] != true || deleted["policyId"] != "p-crud-boundary" {
		t.Fatalf("deleted document: %s", body)
	}

	// The tombstone is terminal and invisible: the id is gone from the projection and
	// both write verbs answer the oracle's 404.
	if listed := listProcessPolicies(t, processes, collection, token); len(listed) != 0 {
		t.Fatalf("a deleted policy must not be projected: %+v", listed)
	}
	status, body = processes.request(t, http.MethodPatch, collection+"/p-crud-boundary", policyProcessHost, token,
		[]byte(`{"name":"after delete"}`))
	if status != http.StatusNotFound || !strings.Contains(string(body), "Backup policy not found") {
		t.Fatalf("update after delete: status=%d body=%s", status, body)
	}
	status, body = processes.request(t, http.MethodDelete, collection+"/p-crud-boundary", policyProcessHost, token, nil)
	if status != http.StatusNotFound || !strings.Contains(string(body), "Backup policy not found") {
		t.Fatalf("delete after delete: status=%d body=%s", status, body)
	}

	// A create that asks for a revision this store cannot adopt is refused by name, so
	// the successful path above is not the only shape the route accepts.
	status, body = processes.request(t, http.MethodPost, collection, policyProcessHost, token,
		[]byte(`{"policyId":"p-crud-bad-revision","policyRevision":9,"name":"nope"}`))
	if status != http.StatusBadRequest || !strings.Contains(string(body), "must start at policyRevision 1") {
		t.Fatalf("create with a foreign revision: status=%d body=%s", status, body)
	}
	processes.assertWriteAdmissionRefusals(t, collection)
}

func listProcessPolicies(t *testing.T, processes *nativeProcesses, collection, token string) []map[string]any {
	t.Helper()
	status, body := processes.request(t, http.MethodGet, collection, policyProcessHost, token, nil)
	if status != http.StatusOK {
		t.Fatalf("policy list over Rust→Go: status=%d body=%s", status, body)
	}
	var listed struct {
		Policies []map[string]any `json:"policies"`
	}
	if err := json.Unmarshal(body, &listed); err != nil {
		t.Fatal(err)
	}
	return listed.Policies
}
