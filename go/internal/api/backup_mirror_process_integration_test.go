//go:build native_integration

package api

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// The typed recipient read must work between separately built production
// processes, not merely between a Rust route and a scripted gRPC fixture.
// Everything written here is in disposable test directories. Python is absent
// from the process tree; this does not qualify the whole product's cutover.
func TestBackupMirrorRustGatewayToGoRecipientsProcess(t *testing.T) {
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", policyProcessSecret)
	control := applyStore(t)
	storeDir := filepath.Dir(control.DatabasePath())
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	workspace := t.TempDir()
	processes := startNativeProcessesWithWorkspace(t, storeDir, 0, true, workspace)
	const recipient = "age1fu59d59ghmr8x2t5dyzjs9xdcjgnakujp7mjy7cz2v7fq6vjqypskh4e62"
	status, body := processes.request(t, http.MethodPost, "/api/workspace/backup-policies",
		policyProcessHost, policyProcessSecret,
		[]byte(`{"policyId":"p-mirror-process","name":"sealed mirror process","enabled":true,"protection":{"mode":"age-recipient","recipients":["`+recipient+`"]}}`))
	if status != http.StatusOK {
		t.Fatalf("policy create: %d %s", status, body)
	}
	const mirror = "/api/workspace/backup-mirrors/process_mirror"
	const envelopeBody = `{"conflicts":[],"conversations":[{"id":"c-process-mirror"}],"schemaVersion":1}`
	var envelope map[string]any
	if err := json.Unmarshal([]byte(envelopeBody), &envelope); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte(envelopeBody))
	envelope["digest"] = hex.EncodeToString(digest[:])
	upload, err := json.Marshal(map[string]any{
		"envelope": envelope, "sourceEpoch": "epoch-process-1", "clientSequence": 1,
		"clientReplicaId": "process-replica",
	})
	if err != nil {
		t.Fatal(err)
	}
	status, body = processes.request(t, http.MethodPut, mirror+"/frontend", policyProcessHost, policyProcessSecret, upload)
	if status != http.StatusOK {
		t.Fatalf("mirror seal via real control/v1 RPC: %d %s", status, body)
	}
	var metadata struct {
		GenerationID      string `json:"generationId"`
		CreationVerified  bool   `json:"creationVerified"`
		RecipientVariants []struct {
			Filename string `json:"filename"`
			SHA256   string `json:"ciphertextSha256"`
		} `json:"recipientVariants"`
	}
	if json.Unmarshal(body, &metadata) != nil || !metadata.CreationVerified || metadata.GenerationID == "" || len(metadata.RecipientVariants) != 1 {
		t.Fatalf("sealed generation: %s", body)
	}
	variant := metadata.RecipientVariants[0]
	ciphertext, err := os.ReadFile(filepath.Join(workspace, ".backup-mirror", "process_mirror", "generations", metadata.GenerationID, variant.Filename))
	if err != nil {
		t.Fatal(err)
	}
	actual := sha256.Sum256(ciphertext)
	if hex.EncodeToString(actual[:]) != variant.SHA256 || !strings.HasPrefix(string(ciphertext), "age-encryption.org/v1\n") || strings.Contains(string(ciphertext), "c-process-mirror") {
		t.Fatal("published mirror does not contain the reported age ciphertext")
	}
	assertProcessMirrorCurrent(t, processes, mirror, metadata.GenerationID)
	before := processMirrorFiles(t, workspace)
	status, body = processes.request(t, http.MethodPut, mirror+"/frontend", policyProcessHost, policyProcessSecret, upload)
	if status != http.StatusOK || !strings.Contains(string(body), `"idempotent":true`) || !reflect.DeepEqual(before, processMirrorFiles(t, workspace)) {
		t.Fatalf("identical replay must leave ciphertext and HEAD unchanged: %d %s", status, body)
	}
	processes.assertReadAdmissionRefusals(t, "/api/workspace/backup-mirrors")
	processes.assertReadAdmissionRefusals(t, mirror)
	for _, check := range []struct {
		host, token string
		want        int
	}{
		{"foreign.example", policyProcessSecret, http.StatusForbidden},
		{policyProcessHost, "", http.StatusUnauthorized},
	} {
		status, body = processes.request(t, http.MethodPut, mirror+"/frontend", check.host, check.token, upload)
		if status != check.want || !reflect.DeepEqual(before, processMirrorFiles(t, workspace)) {
			t.Fatalf("upload admission refusal: %d %s", status, body)
		}
	}
	// A strong process kill tests persistent mirror recovery, then loss of the
	// authoritative RPC must refuse both status and PUT without changing a byte.
	processes.stopGateway()
	processes.stopDaemon()
	previousToken, leaseUntil := processControlWriter(t, storeDir)
	remaining := time.Until(time.Unix(leaseUntil, 0).Add(100 * time.Millisecond))
	if remaining > 35*time.Second {
		t.Fatalf("unexpected abandoned writer lease: %v", remaining)
	}
	if remaining > 0 {
		t.Logf("Waiting %s for the actual durable writer lease after force-kill", remaining)
		time.Sleep(remaining)
	}
	restarted := startNativeProcessesWithWorkspace(t, storeDir, 0, true, workspace)
	newToken, _ := processControlWriter(t, storeDir)
	if newToken <= previousToken {
		t.Fatal("successor did not claim a new durable writer fence")
	}
	assertProcessMirrorCurrent(t, restarted, mirror, metadata.GenerationID)
	if !reflect.DeepEqual(before, processMirrorFiles(t, workspace)) {
		t.Fatal("restart changed the immutable sealed generation")
	}
	restarted.stopDaemon()
	for _, method := range []string{http.MethodGet, http.MethodPut} {
		path := mirror
		var payload []byte
		if method == http.MethodPut {
			path += "/frontend"
			payload = upload
		}
		status, body = restarted.request(t, method, path, policyProcessHost, policyProcessSecret, payload)
		if status != http.StatusServiceUnavailable || !strings.Contains(string(body), "NATIVE_MIRROR_RECIPIENT_SOURCE_UNAVAILABLE") || !reflect.DeepEqual(before, processMirrorFiles(t, workspace)) {
			t.Fatalf("lost authoritative recipient RPC must refuse with no effects: %s %d %s", method, status, body)
		}
	}
}

// Read only: a successor must wait for the killed writer's recorded lease,
// rather than changing SQL, the clock, or production lease defaults in a test.
func processControlWriter(t *testing.T, directory string) (int64, int64) {
	t.Helper()
	databasePath := filepath.ToSlash(filepath.Join(directory, store.ControlDatabaseFilename))
	// RFC 8089's local file URI needs a leading slash before a Windows drive;
	// otherwise net/url renders C: as an authority which SQLite refuses.
	if !strings.HasPrefix(databasePath, "/") {
		databasePath = "/" + databasePath
	}
	origin := url.URL{Scheme: "file", Path: databasePath}
	query := origin.Query()
	query.Set("mode", "ro")
	origin.RawQuery = query.Encode()
	database, err := sql.Open("sqlite", origin.String())
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	var token, leaseUntil int64
	if err := database.QueryRow("SELECT fencing_token, lease_until FROM control_writer WHERE singleton=1").Scan(&token, &leaseUntil); err != nil {
		t.Fatal(err)
	}
	return token, leaseUntil
}

func assertProcessMirrorCurrent(t *testing.T, processes *nativeProcesses, mirror, generation string) {
	t.Helper()
	status, body := processes.request(t, http.MethodGet, mirror, policyProcessHost, policyProcessSecret, nil)
	var snapshot map[string]any
	if status != http.StatusOK || json.Unmarshal(body, &snapshot) != nil || snapshot["status"] != "current" {
		t.Fatalf("mirror status: %d %s", status, body)
	}
	metadata, ok := snapshot["mirror"].(map[string]any)
	if !ok || metadata["generationId"] != generation {
		t.Fatalf("mirror status generation: %s", body)
	}
	status, body = processes.request(t, http.MethodGet, "/api/workspace/backup-mirrors", policyProcessHost, policyProcessSecret, nil)
	var listed struct {
		Mirrors []map[string]any `json:"mirrors"`
	}
	if status != http.StatusOK || json.Unmarshal(body, &listed) != nil || len(listed.Mirrors) != 1 || listed.Mirrors[0]["generationId"] != generation {
		t.Fatalf("mirror list: %d %s", status, body)
	}
}

func processMirrorFiles(t *testing.T, workspace string) map[string]string {
	t.Helper()
	root := filepath.Join(workspace, ".backup-mirror")
	files := map[string]string{}
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		digest := sha256.Sum256(raw)
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		files[relative] = hex.EncodeToString(digest[:])
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return files
}
