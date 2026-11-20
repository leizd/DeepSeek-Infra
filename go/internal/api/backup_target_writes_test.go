package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func targetWriteSetup(t *testing.T, control *store.Control) (*httptest.Server, *http.Client, string) {
	t.Helper()
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-write-secret")
	t.Setenv("AUTH_ALLOWED_HOSTS", "")
	t.Setenv("HOST", "127.0.0.1")
	base := t.TempDir()
	// Resolve TempDir symlinks (e.g. Windows 8.3 short paths or macOS /var -> /private/var).
	if eval, err := filepath.EvalSymlinks(base); err == nil {
		base = eval
	}
	runtimeRoot := filepath.Join(base, "runtime")
	fakeTemp := filepath.Join(base, "sys-temp")
	if err := os.MkdirAll(runtimeRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(fakeTemp, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DEEPSEEK_INFRA_ROOT", runtimeRoot)
	prevTemp := backupTargetTempDir
	backupTargetTempDir = func() string { return fakeTemp }
	t.Cleanup(func() { backupTargetTempDir = prevTemp })

	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server, server.Client(), base
}

func sendTargetReq(t *testing.T, client *http.Client, method, url, body string) (int, map[string]any) {
	t.Helper()
	return sendTargetReqWith(t, client, method, url, "127.0.0.1", "target-write-secret", body)
}

func sendTargetReqWith(t *testing.T, client *http.Client, method, url, host, token, body string) (int, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(method, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Host = host
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var parsed map[string]any
	dec := json.NewDecoder(resp.Body)
	dec.UseNumber()
	_ = dec.Decode(&parsed)
	return resp.StatusCode, parsed
}

func writeMarkerUpdates(t *testing.T, dir string, updates map[string]any) {
	t.Helper()
	path := filepath.Join(dir, targetMarkerName)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	for k, v := range updates {
		doc[k] = v
	}
	out, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, out, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestBackupTargetCreateReuseCloneMovedAndRegisterNew(t *testing.T) {
	control := targetHealthPublicStore(t, true, 1000)
	server, client, base := targetWriteSetup(t, control)

	usbDir := filepath.Join(base, "usb-a")
	if err := os.MkdirAll(usbDir, 0o755); err != nil {
		t.Fatal(err)
	}
	createPayload := `{"path":` + mustJSON(usbDir) + `,"label":"USB A","region":"us-west","failureDomain":"rack-1","provider":"local","jurisdiction":"US","priority":5,"costClass":"archive","storageCostPerGiBMonth":0.02,"egressCostPerGiB":0.01,"maxReadBytesPerSecond":1048576,"maxWriteBytesPerSecond":2097152,"maxConcurrentTransfers":2}`
	status, created := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", createPayload)
	if status != http.StatusOK {
		t.Fatalf("create failed: %d %v", status, created)
	}
	targetID, _ := created["targetId"].(string)
	if !strings.HasPrefix(targetID, "target_") || created["drainState"] != "active" || created["priority"] != json.Number("5") {
		t.Fatalf("unexpected created target: %v", created)
	}

	// Re-POST on the same directory reuses the registered target ID and advances revision.
	status, reused := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(usbDir)+`,"label":"USB A v2"}`)
	if status != http.StatusOK || reused["targetId"] != targetID || reused["label"] != "USB A v2" {
		t.Fatalf("reuse failed: %d %v", status, reused)
	}

	// Clone detection when marker is copied while original path still exists.
	cloneDir := filepath.Join(base, "usb-clone")
	if err := os.MkdirAll(cloneDir, 0o755); err != nil {
		t.Fatal(err)
	}
	markerBytes, err := os.ReadFile(filepath.Join(usbDir, targetMarkerName))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cloneDir, targetMarkerName), markerBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	status, cloneErr := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(cloneDir)+`}`)
	if status != http.StatusConflict || !strings.Contains(cloneErr["error"].(string), "target-clone-detected") {
		t.Fatalf("expected clone conflict: %d %v", status, cloneErr)
	}

	// Moved disk succeeds once old path disappears.
	if err := os.RemoveAll(usbDir); err != nil {
		t.Fatal(err)
	}
	status, moved := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(cloneDir)+`}`)
	if status != http.StatusOK || moved["targetId"] != targetID || moved["path"] != cloneDir {
		t.Fatalf("moved target failed: %d %v", status, moved)
	}

	// List returns active targets.
	status, listed := sendTargetReq(t, client, http.MethodGet, server.URL+"/api/workspace/backup-targets", "")
	if status != http.StatusOK {
		t.Fatalf("list failed: %d %v", status, listed)
	}
	items, _ := listed["targets"].([]any)
	if len(items) != 1 {
		t.Fatalf("expected 1 listed target: %v", listed)
	}

	// register-new issues a distinct targetId on the same directory.
	status, regNew := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/register-new", `{"path":`+mustJSON(cloneDir)+`,"label":"Fresh Identity"}`)
	if status != http.StatusOK || regNew["targetId"] == targetID || regNew["label"] != "Fresh Identity" {
		t.Fatalf("register-new failed: %d %v", status, regNew)
	}

	// Non-filesystem kind or invalid QoS in register-new vs create.
	status, badNew := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/register-new", `{"path":`+mustJSON(filepath.Join(base, "missing"))+`}`)
	if status != http.StatusBadRequest {
		t.Fatalf("register-new missing path: %d %v", status, badNew)
	}
	status, badQos := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/register-new", `{"path":`+mustJSON(cloneDir)+`,"maxConcurrentTransfers":0}`)
	if status != http.StatusBadRequest {
		t.Fatalf("register-new bad qos: %d %v", status, badQos)
	}
}

func TestBackupTargetCreateRefusalsAndValidation(t *testing.T) {
	control := targetHealthPublicStore(t, true, 1000)
	server, client, base := targetWriteSetup(t, control)
	url := server.URL + "/api/workspace/backup-targets"

	goodDir := filepath.Join(base, "good")
	if err := os.MkdirAll(goodDir, 0o755); err != nil {
		t.Fatal(err)
	}

	// Auth and host failures on writes.
	status, _ := sendTargetReqWith(t, client, http.MethodPost, url, "evil.example.com", "target-write-secret", `{}`)
	if status != http.StatusForbidden {
		t.Fatalf("expected 403 host, got %d", status)
	}
	status, _ = sendTargetReqWith(t, client, http.MethodPost, url, "127.0.0.1", "wrong", `{}`)
	if status != http.StatusUnauthorized {
		t.Fatalf("expected 401 auth, got %d", status)
	}
	status, _ = sendTargetReqWith(t, client, http.MethodPost, url+"/register-new", "127.0.0.1", "wrong", `{}`)
	if status != http.StatusUnauthorized {
		t.Fatalf("expected 401 auth on register-new, got %d", status)
	}

	cases := []struct {
		name string
		body string
		want int
		sub  string
	}{
		{"secret top", `{"secretAccessKey":"leaked"}`, http.StatusBadRequest, "credentials"},
		{"secret slice", `{"nested":[{"password":"abc"}]}`, http.StatusBadRequest, "credentials"},
		{"webdav", `{"kind":"webdav"}`, http.StatusNotImplemented, "WebDAV"},
		{"s3 missing bucket", `{"kind":"s3"}`, http.StatusBadRequest, "bucket"},
		{"s3 valid unmigrated", `{"kind":"s3","bucket":"bkt"}`, http.StatusServiceUnavailable, "s3TargetAvailable=false"},
		{"bad kind", `{"kind":"ftp"}`, http.StatusBadRequest, "unsupported"},
		{"relative path", `{"path":"rel/dir"}`, http.StatusBadRequest, "absolute"},
		{"missing path", `{"path":` + mustJSON(filepath.Join(base, "nope")) + `}`, http.StatusBadRequest, "does not exist"},
		{"inside runtime root", `{"path":` + mustJSON(filepath.Join(base, "runtime")) + `}`, http.StatusBadRequest, "runtime root"},
		{"inside temp root", `{"path":` + mustJSON(filepath.Join(base, "sys-temp")) + `}`, http.StatusBadRequest, "temporary"},
		{"bad cost", `{"path":` + mustJSON(goodDir) + `,"storageCostPerGiBMonth":-1}`, http.StatusBadRequest, "non-negative"},
		{"bad cost string", `{"path":` + mustJSON(goodDir) + `,"egressCostPerGiB":"nan"}`, http.StatusBadRequest, "non-negative"},
		{"bad qos", `{"path":` + mustJSON(goodDir) + `,"maxReadBytesPerSecond":0}`, http.StatusBadRequest, "positive integer"},
		{"bad write qos", `{"path":` + mustJSON(goodDir) + `,"maxWriteBytesPerSecond":1.5}`, http.StatusBadRequest, "positive integer"},
		{"bad priority", `{"path":` + mustJSON(goodDir) + `,"priority":1.5}`, http.StatusBadRequest, "integer"},
		{"bad payload json", `{bad`, http.StatusBadRequest, "JSON object"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			code, resp := sendTargetReq(t, client, http.MethodPost, url, tc.body)
			if code != tc.want || !strings.Contains(resp["error"].(string), tc.sub) {
				t.Fatalf("%s: got (%d, %v), want (%d, %q)", tc.name, code, resp, tc.want, tc.sub)
			}
		})
	}

	// Corrupt marker and invalid marker targetId.
	badMarkerDir := filepath.Join(base, "bad-marker")
	if err := os.MkdirAll(badMarkerDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(badMarkerDir, targetMarkerName), []byte("{not-json"), 0o600); err != nil {
		t.Fatal(err)
	}
	code, resp := sendTargetReq(t, client, http.MethodPost, url, `{"path":`+mustJSON(badMarkerDir)+`}`)
	if code != http.StatusBadRequest || !strings.Contains(resp["error"].(string), "unreadable") {
		t.Fatalf("bad marker json: %d %v", code, resp)
	}
	if err := os.WriteFile(filepath.Join(badMarkerDir, targetMarkerName), []byte(`{"schemaVersion":3}`), 0o600); err != nil {
		t.Fatal(err)
	}
	code, resp = sendTargetReq(t, client, http.MethodPost, url, `{"path":`+mustJSON(badMarkerDir)+`}`)
	if code != http.StatusBadRequest || !strings.Contains(resp["error"].(string), "invalid") {
		t.Fatalf("marker missing targetId: %d %v", code, resp)
	}

	// Symlink / reparse point rejection + overlap rejection.
	parentDir := filepath.Join(base, "parent")
	childDir := filepath.Join(parentDir, "child")
	if err := os.MkdirAll(childDir, 0o755); err != nil {
		t.Fatal(err)
	}
	code, created := sendTargetReq(t, client, http.MethodPost, url, `{"path":`+mustJSON(parentDir)+`}`)
	if code != http.StatusOK {
		t.Fatalf("create parent: %d %v", code, created)
	}
	code, overlap := sendTargetReq(t, client, http.MethodPost, url, `{"path":`+mustJSON(childDir)+`}`)
	if code != http.StatusBadRequest || !strings.Contains(overlap["error"].(string), "overlaps") {
		t.Fatalf("expected overlap: %d %v", code, overlap)
	}

	notADir := filepath.Join(base, "file.txt")
	if err := os.WriteFile(notADir, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	code, fileResp := sendTargetReq(t, client, http.MethodPost, url, `{"path":`+mustJSON(notADir)+`}`)
	if code != http.StatusBadRequest || !strings.Contains(fileResp["error"].(string), "does not exist") {
		t.Fatalf("regular file path expected 400: %d %v", code, fileResp)
	}

	linkPath := filepath.Join(base, "link-dir")
	var linkCreated bool
	if err := os.Symlink(goodDir, linkPath); err == nil {
		linkCreated = true
	} else if runtime.GOOS == "windows" {
		if out, jErr := exec.Command("cmd.exe", "/d", "/c", "mklink", "/J", linkPath, goodDir).CombinedOutput(); jErr == nil {
			linkCreated = true
		} else {
			t.Logf("skip junction check: %v %s", jErr, string(out))
		}
	}
	if linkCreated {
		t.Cleanup(func() { _ = os.Remove(linkPath) })
		code, symResp := sendTargetReq(t, client, http.MethodPost, url, `{"path":`+mustJSON(linkPath)+`}`)
		if code != http.StatusBadRequest || !strings.Contains(symResp["error"].(string), "symlink") {
			t.Fatalf("expected symlink rejection: %d %v", code, symResp)
		}
	}

	// Nil control & pre-cutover refusals.
	nilSrv, nilCli, _ := targetWriteSetup(t, nil)
	code, _ = sendTargetReq(t, nilCli, http.MethodPost, nilSrv.URL+"/api/workspace/backup-targets", `{}`)
	if code != http.StatusServiceUnavailable {
		t.Fatalf("nil control create: %d", code)
	}
	code, _ = sendTargetReq(t, nilCli, http.MethodPost, nilSrv.URL+"/api/workspace/backup-targets/register-new", `{}`)
	if code != http.StatusServiceUnavailable {
		t.Fatalf("nil control regnew: %d", code)
	}
	code, _ = sendTargetReq(t, nilCli, http.MethodDelete, nilSrv.URL+"/api/workspace/backup-targets/target_1", "")
	if code != http.StatusServiceUnavailable {
		t.Fatalf("nil control delete: %d", code)
	}

	shadowCtrl, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "shadow-owner", Now: func() int64 { return 1000 }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = shadowCtrl.Close() })
	shSrv, shCli, _ := targetWriteSetup(t, shadowCtrl)
	code, shCreate := sendTargetReq(t, shCli, http.MethodPost, shSrv.URL+"/api/workspace/backup-targets", `{}`)
	if code != http.StatusServiceUnavailable || shCreate["code"] != CodeControlNotAuthoritative {
		t.Fatalf("shadow create: %d %v", code, shCreate)
	}
	code, shProbe := sendTargetReq(t, shCli, http.MethodPost, shSrv.URL+"/api/workspace/backup-targets/target_1/probe", "")
	if code != http.StatusServiceUnavailable || shProbe["code"] != CodeControlNotAuthoritative {
		t.Fatalf("shadow probe: %d %v", code, shProbe)
	}
	code, shReg := sendTargetReq(t, shCli, http.MethodPost, shSrv.URL+"/api/workspace/backup-targets/register-new", `{"path":`+mustJSON(goodDir)+`}`)
	if code != http.StatusServiceUnavailable || shReg["code"] != CodeControlNotAuthoritative {
		t.Fatalf("shadow register-new: %d %v", code, shReg)
	}
}

func TestBackupTargetDeleteAdoptActivateAndProbe(t *testing.T) {
	control := targetHealthPublicStore(t, true, 1000)
	server, client, base := targetWriteSetup(t, control)

	usbDir := filepath.Join(base, "usb-life")
	if err := os.MkdirAll(usbDir, 0o755); err != nil {
		t.Fatal(err)
	}
	code, created := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(usbDir)+`}`)
	if code != http.StatusOK {
		t.Fatalf("create: %d %v", code, created)
	}
	targetID := created["targetId"].(string)

	// Missing target responses and auth failure on loadAuthoritativeTarget.
	code, _ = sendTargetReqWith(t, client, http.MethodDelete, server.URL+"/api/workspace/backup-targets/target_missing", "127.0.0.1", "wrong", "")
	if code != http.StatusUnauthorized {
		t.Fatalf("delete unauthorized: %d", code)
	}
	code, _ = sendTargetReq(t, client, http.MethodDelete, server.URL+"/api/workspace/backup-targets/target_missing", "")
	if code != http.StatusNotFound {
		t.Fatalf("delete missing: %d", code)
	}
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/target_missing/adopt", "")
	if code != http.StatusNotFound {
		t.Fatalf("adopt missing: %d", code)
	}
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/target_missing/activate", "")
	if code != http.StatusNotFound {
		t.Fatalf("activate missing: %d", code)
	}
	code, missingProbe := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/target_missing/probe", "")
	if code != http.StatusOK || missingProbe["ready"] != false || missingProbe["detail"] != "Backup target not found" {
		t.Fatalf("probe missing: %d %v", code, missingProbe)
	}

	// Probe healthy target -> ok.
	code, probe := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || probe["ready"] != true || probe["status"] != "ok" || probe["path"] != usbDir {
		t.Fatalf("probe ok: %d %v", code, probe)
	}

	// Probe advances checkpoint when generation rises cleanly, then detects rollback & adopts.
	writeMarkerUpdates(t, usbDir, map[string]any{"targetGeneration": 5, "latestCommitHash": strings.Repeat("a", 64)})
	code, probe = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || probe["ready"] != true {
		t.Fatalf("probe advance failed: %d %v", code, probe)
	}
	writeMarkerUpdates(t, usbDir, map[string]any{"targetGeneration": 2, "latestCommitHash": strings.Repeat("b", 64)})
	code, rb := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || rb["ready"] != false || rb["status"] != "target-rollback-detected" {
		t.Fatalf("rollback detection failed: %d %v", code, rb)
	}
	code, adopted := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/adopt", "")
	if code != http.StatusOK || adopted["adopted"] != true || adopted["incarnationId"] == created["incarnationId"] {
		t.Fatalf("adopt failed: %d %v", code, adopted)
	}
	code, probe = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || probe["ready"] != true {
		t.Fatalf("probe after adopt failed: %d %v", code, probe)
	}

	// Fork detection: incarnation change and same-generation head divergence.
	writeMarkerUpdates(t, usbDir, map[string]any{"incarnationId": "inc_foreign12345678"})
	code, fork1 := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || fork1["status"] != "target-fork-detected" {
		t.Fatalf("fork incarnation: %d %v", code, fork1)
	}
	code, adopted = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/adopt", "")
	if code != http.StatusOK {
		t.Fatalf("re-adopt failed: %d %v", code, adopted)
	}
	writeMarkerUpdates(t, usbDir, map[string]any{"latestCommitHash": strings.Repeat("c", 64)})
	code, fork2 := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || fork2["status"] != "target-fork-detected" {
		t.Fatalf("fork commit divergence: %d %v", code, fork2)
	}

	// Marker nonce mismatch + missing marker errors on adopt & probe.
	writeMarkerUpdates(t, usbDir, map[string]any{"targetNonce": strings.Repeat("0", 32)})
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/adopt", "")
	if code != http.StatusBadRequest {
		t.Fatalf("adopt mismatch expected 400, got %d", code)
	}
	code, swProbe := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || swProbe["status"] != "blocked-target-unavailable" {
		t.Fatalf("probe swap: %d %v", code, swProbe)
	}
	if err := os.Remove(filepath.Join(usbDir, targetMarkerName)); err != nil {
		t.Fatal(err)
	}
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/adopt", "")
	if code != http.StatusBadRequest {
		t.Fatalf("adopt missing marker expected 400, got %d", code)
	}
	code, noMarkerProbe := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || noMarkerProbe["ready"] != false {
		t.Fatalf("probe missing marker: %d %v", code, noMarkerProbe)
	}

	// Activate from draining state restores ACTIVE state and strips drain fields.
	rec, found, err := control.Get("target", targetID)
	if err != nil || !found {
		t.Fatalf("get target: %v %v", found, err)
	}
	doc, err := decodeTargetRecord(rec)
	if err != nil {
		t.Fatal(err)
	}
	for _, drainState := range []struct{ jsonState, recState string }{
		{"disabled", "DISABLED"},
		{"active", "ACTIVE"},
		{"draining", "DRAINING"},
		{"drained", "DRAINED"},
	} {
		rec, _, _ = control.Get("target", targetID)
		nextRev := rec.Revision + 1
		doc["drainState"] = drainState.jsonState
		doc["drainReason"] = "maintenance"
		doc["topologyGeneration"] = nextRev
		encoded, _ := json.Marshal(doc)
		if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
			OperationID: newOperationID(),
			ActionID:    newActionID(),
			Actor:       "test",
			Record:      store.Record{Domain: "target", ID: targetID, Revision: nextRev, State: drainState.recState, Payload: encoded},
		}); err != nil {
			t.Fatalf("set %s: %v", drainState.recState, err)
		}
		if targetRecordState(doc) != drainState.recState {
			t.Fatalf("targetRecordState mismatch for %s", drainState.jsonState)
		}
	}
	code, activated := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/activate", "")
	if code != http.StatusOK || activated["drainState"] != "active" || activated["drainReason"] != nil {
		t.Fatalf("activate failed: %d %v", code, activated)
	}

	// S3 target record fail-closed on adopt & probe.
	s3Payload, _ := json.Marshal(map[string]any{"targetId": "target_s3_only", "kind": "s3", "bucket": "bkt", "topologyGeneration": 1})
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: newOperationID(),
		ActionID:    newActionID(),
		Actor:       "test",
		Record:      store.Record{Domain: "target", ID: "target_s3_only", Revision: 1, State: "ACTIVE", Payload: s3Payload},
	}); err != nil {
		t.Fatal(err)
	}
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/target_s3_only/adopt", "")
	if code != http.StatusBadRequest {
		t.Fatalf("adopt s3 expected 400, got %d", code)
	}
	code, s3Probe := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/target_s3_only/probe", "")
	if code != http.StatusOK || s3Probe["ready"] != false || s3Probe["status"] != "blocked-s3-unmigrated" {
		t.Fatalf("probe s3: %d %v", code, s3Probe)
	}

	// Adopt succeeds when marker omits targetGeneration; probe catches unsafe containment.
	markerPath := filepath.Join(usbDir, targetMarkerName)
	noGenMarker := map[string]any{
		"schemaVersion":    3,
		"targetId":         targetID,
		"targetNonce":      created["targetNonce"],
		"incarnationId":    adopted["incarnationId"],
		"latestCommitHash": strings.Repeat("a", 64),
	}
	if b, mErr := json.MarshalIndent(noGenMarker, "", "  "); mErr == nil {
		_ = os.WriteFile(markerPath, b, 0o600)
	}
	code, adoptedNoGen := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/adopt", "")
	if code != http.StatusOK || adoptedNoGen["adopted"] != true {
		t.Fatalf("adopt without generation failed: %d %v", code, adoptedNoGen)
	}
	t.Setenv("DEEPSEEK_INFRA_ROOT", base)
	code, unsafeProbe := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/probe", "")
	if code != http.StatusOK || unsafeProbe["status"] != "blocked-unsafe-target" {
		t.Fatalf("unsafe probe expected blocked-unsafe-target: %d %v", code, unsafeProbe)
	}
	t.Setenv("DEEPSEEK_INFRA_ROOT", filepath.Join(base, "runtime"))

	// Unreadable marker & corrupt JSON payload in store.
	_ = os.WriteFile(markerPath, []byte("{bad"), 0o600)
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/"+targetID+"/adopt", "")
	if code != http.StatusBadRequest {
		t.Fatalf("corrupt marker adopt expected 400, got %d", code)
	}
	rec, _, _ = control.Get("target", targetID)
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: newOperationID(),
		ActionID:    newActionID(),
		Actor:       "test",
		Record:      store.Record{Domain: "target", ID: targetID, Revision: rec.Revision + 1, State: "ACTIVE", Payload: []byte(`{"not":"valid"}`)},
	}); err == nil {
		t.Log("unexpected success for payload missing topologyGeneration")
	}

	// Target health untransferred error maps to 503 TARGET_HEALTH_NOT_TRANSFERRED.
	rr := httptest.NewRecorder()
	targetStoreWriteRefusal(rr, store.ErrTargetHealthNotTransferred)
	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("target health untransferred expected 503, got %d", rr.Code)
	}

	// configuredRuntimeRoot infers runtime root from .go-control directory when DEEPSEEK_INFRA_ROOT is empty.
	t.Setenv("DEEPSEEK_INFRA_ROOT", "")
	dbParent := filepath.Join(base, "inferred-root")
	goControlDir := filepath.Join(dbParent, ".go-control")
	if err := os.MkdirAll(goControlDir, 0o755); err != nil {
		t.Fatal(err)
	}
	inferredCtrl, err := store.OpenControl(store.OpenOptions{Path: goControlDir, Owner: "infer-owner", Now: func() int64 { return 1000 }})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = inferredCtrl.Close() })
	if gotRoot := configuredRuntimeRoot(inferredCtrl); gotRoot != dbParent {
		t.Fatalf("configuredRuntimeRoot = %q, want %q", gotRoot, dbParent)
	}

	// Create target when non-filesystem S3 record exists in store, and test v1 marker defaults + write error.
	usbV1 := filepath.Join(base, "usb-v1")
	if err := os.MkdirAll(usbV1, 0o755); err != nil {
		t.Fatal(err)
	}
	v1Marker := `{"schemaVersion":1,"targetId":"target_v1leg","targetNonce":"0123456789abcdef0123456789abcdef"}`
	if err := os.WriteFile(filepath.Join(usbV1, targetMarkerName), []byte(v1Marker), 0o600); err != nil {
		t.Fatal(err)
	}
	code, v1Created := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(usbV1)+`}`)
	if code != http.StatusOK || v1Created["targetId"] != "target_v1leg" || v1Created["lastSeenCommitHash"] != targetGenesisHash {
		t.Fatalf("v1 marker create failed: %d %v", code, v1Created)
	}
	writeMarkerUpdates(t, usbV1, map[string]any{"incarnationId": "inc_v1backfill00"})
	code, v1Probe := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/target_v1leg/probe", "")
	if code != http.StatusOK || v1Probe["ready"] != true {
		t.Fatalf("v1 backfill probe failed: %d %v", code, v1Probe)
	}

	failWriteDir := filepath.Join(base, "fail-marker-dir")
	if err := os.MkdirAll(filepath.Join(failWriteDir, targetMarkerName), 0o755); err != nil {
		t.Fatal(err)
	}
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/register-new", `{"path":`+mustJSON(failWriteDir)+`}`)
	if code != http.StatusInternalServerError {
		t.Fatalf("register-new with directory at marker path expected 500, got %d", code)
	}

	// Non-existent DEEPSEEK_INFRA_ROOT falls back to filepath.Clean.
	t.Setenv("DEEPSEEK_INFRA_ROOT", filepath.Join(base, "does-not-exist-yet"))
	if got := configuredRuntimeRoot(control); !strings.HasSuffix(got, "does-not-exist-yet") {
		t.Fatalf("configuredRuntimeRoot fallback mismatch: %q", got)
	}
	t.Setenv("DEEPSEEK_INFRA_ROOT", filepath.Join(base, "runtime"))

	// Unreadable marker directory (covers non-ENOENT read error) & decodeTargetRecord error check.
	dirAsMarker := filepath.Join(usbV1, "marker-is-dir")
	if err := os.MkdirAll(filepath.Join(dirAsMarker, targetMarkerName), 0o755); err != nil {
		t.Fatal(err)
	}
	if _, _, _, rErr := readFilesystemTargetMarker(dirAsMarker); rErr == nil || !strings.Contains(rErr.Error(), "unreadable") {
		t.Fatalf("directory marker expected unreadable, got %v", rErr)
	}
	if _, _, _, rErr := readFilesystemTargetMarker(filepath.Join(base, "no-such-dir")); rErr == nil {
		t.Fatalf("missing dir expected error")
	}
	if _, decErr := decodeTargetRecord(store.Record{Payload: []byte("{bad")}); decErr == nil {
		t.Fatal("expected decodeTargetRecord error")
	}

	// atomicWriteTargetMarker failure when tmp is an existing directory & pre-existing marker inside runtimeRoot.
	failOpenDir := filepath.Join(base, "fail-open-tmp")
	tmpBlock := filepath.Join(failOpenDir, "."+targetMarkerName+"."+strings.TrimSpace(fmt.Sprintf("%d", os.Getpid()))+".tmp")
	if err := os.MkdirAll(tmpBlock, 0o755); err != nil {
		t.Fatal(err)
	}
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/register-new", `{"path":`+mustJSON(failOpenDir)+`}`)
	if code != http.StatusInternalServerError {
		t.Fatalf("register-new with dir at tmp expected 500, got %d", code)
	}
	markerInsideRoot := filepath.Join(base, "runtime", "inside-marked")
	if err := os.MkdirAll(markerInsideRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(markerInsideRoot, targetMarkerName), []byte(v1Marker), 0o600); err != nil {
		t.Fatal(err)
	}
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(markerInsideRoot)+`}`)
	if code != http.StatusBadRequest {
		t.Fatalf("marked target inside runtime root expected 400, got %d", code)
	}
	shRegCode, _ := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/register-new", `{"path":`+mustJSON(usbV1)+`,"maxConcurrentTransfers":0}`)
	if shRegCode != http.StatusBadRequest {
		t.Fatalf("register-new bad maxConcurrentTransfers expected 400, got %d", shRegCode)
	}

	// Inject filesystem record without path and non-filesystem S3 record, then register a new target to hit containment skips.
	noPathPayload, _ := json.Marshal(map[string]any{"targetId": "target_nopath", "kind": "filesystem", "topologyGeneration": 1})
	_, _ = control.ApplyOperatorMutation(store.OperatorMutation{
		OperationID: newOperationID(),
		ActionID:    newActionID(),
		Actor:       "test",
		Record:      store.Record{Domain: "target", ID: "target_nopath", Revision: 1, State: "ACTIVE", Payload: noPathPayload},
	})
	usbAfterNoPath := filepath.Join(base, "usb-after-nopath")
	if err := os.MkdirAll(usbAfterNoPath, 0o755); err != nil {
		t.Fatal(err)
	}
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(usbAfterNoPath)+`}`)
	if code != http.StatusOK {
		t.Fatalf("create after noPath failed: %d", code)
	}

	// Delete target and confirm tombstones do not block re-creating a target at the same path.
	code, del := sendTargetReq(t, client, http.MethodDelete, server.URL+"/api/workspace/backup-targets/"+targetID, "")
	if code != http.StatusOK || del["deleted"] != true || del["targetId"] != targetID {
		t.Fatalf("delete failed: %d %v", code, del)
	}
	code, _ = sendTargetReq(t, client, http.MethodDelete, server.URL+"/api/workspace/backup-targets/"+targetID, "")
	if code != http.StatusNotFound {
		t.Fatalf("second delete expected 404, got %d", code)
	}
	// Tombstones must not trigger containment overlap on register-new or child directories.
	childAfterDelete := filepath.Join(usbDir, "reclaimed-child")
	if err := os.MkdirAll(childAfterDelete, 0o755); err != nil {
		t.Fatal(err)
	}
	code, recreated := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(childAfterDelete)+`}`)
	if code != http.StatusOK || recreated["targetId"] == targetID {
		t.Fatalf("create inside deleted target dir failed: %d %v", code, recreated)
	}
	usbReinit := filepath.Join(base, "usb-reinit")
	if err := os.MkdirAll(usbReinit, 0o755); err != nil {
		t.Fatal(err)
	}
	code, c2 := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(usbReinit)+`}`)
	if code != http.StatusOK {
		t.Fatalf("create usbReinit: %d %v", code, c2)
	}
	id2 := c2["targetId"].(string)
	code, _ = sendTargetReq(t, client, http.MethodDelete, server.URL+"/api/workspace/backup-targets/"+id2, "")
	if code != http.StatusOK {
		t.Fatalf("delete usbReinit: %d", code)
	}
	// Re-POST on a tombstoned target fails closed via saveTargetMutation/targetStoreWriteRefusal.
	code, _ = sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets", `{"path":`+mustJSON(usbReinit)+`}`)
	if code != http.StatusConflict {
		t.Fatalf("recreate tombstoned target expected 409, got %d", code)
	}
	code, reReg := sendTargetReq(t, client, http.MethodPost, server.URL+"/api/workspace/backup-targets/register-new", `{"path":`+mustJSON(usbReinit)+`}`)
	if code != http.StatusOK || reReg["targetId"] == id2 {
		t.Fatalf("register-new after delete failed: %d %v", code, reReg)
	}

	// Dispatch methods and unknown subpaths.
	for _, tc := range []struct {
		method, path string
		want         int
	}{
		{http.MethodDelete, "/api/workspace/backup-targets/register-new", http.StatusNotFound},
		{http.MethodPut, "/api/workspace/backup-targets/register-new", http.StatusMethodNotAllowed},
		{http.MethodGet, "/api/workspace/backup-targets/target_x", http.StatusMethodNotAllowed},
		{http.MethodGet, "/api/workspace/backup-targets/target_x/adopt", http.StatusMethodNotAllowed},
		{http.MethodGet, "/api/workspace/backup-targets/target_x/activate", http.StatusMethodNotAllowed},
		{http.MethodGet, "/api/workspace/backup-targets/target_x/probe", http.StatusMethodNotAllowed},
		{http.MethodPost, "/api/workspace/backup-targets/", http.StatusNotImplemented},
		{http.MethodPost, "/api/workspace/backup-targets/target_x/drain", http.StatusNotImplemented},
	} {
		got, _ := sendTargetReq(t, client, tc.method, server.URL+tc.path, "")
		if got != tc.want {
			t.Fatalf("%s %s: got %d, want %d", tc.method, tc.path, got, tc.want)
		}
	}
}

func mustJSON(value any) string {
	raw, _ := json.Marshal(value)
	return string(raw)
}
