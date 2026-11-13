package store

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

const legacyTargetV21Source = "ab09a93b6584d4ff50a1cc351ddf4a5c65e59302"

type legacyTargetLocationFixture struct {
	path, previous string
	scope          BackupTargetRegistrationScope
	marker         *actionv1.FilesystemBackupTargetInspectionReceipt
	document       json.RawMessage
	record         Record
	result         OperatorMutationResult
	boundary       int64
}

func produceLegacyTargetV21Fixture(t *testing.T) legacyTargetLocationFixture {
	t.Helper()
	binary := os.Getenv("DEEPSEEK_TEST_LEGACY_V21_BINARY")
	if !filepath.IsAbs(binary) {
		t.Fatal("the exact-source cloud v21 producer binary is required")
	}
	output := t.TempDir()
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, binary, "-test.run=^TestProduceLegacyV21TargetRegistration$", "-test.count=1", "-test.timeout=60s")
	command.Dir = filepath.Dir(binary)
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(entry, "DEEPSEEK_TEST_V21_OUT=") {
			command.Env = append(command.Env, entry)
		}
	}
	command.Env = append(command.Env, "DEEPSEEK_TEST_V21_OUT="+output)
	if log, err := command.CombinedOutput(); err != nil {
		t.Fatalf("retained v21 producer failed: %v: %s", err, log)
	}
	raw, err := os.ReadFile(filepath.Join(output, "legacy-result.json"))
	if err != nil {
		t.Fatal(err)
	}
	var value struct {
		SourceCommit  string `json:"sourceCommit"`
		SchemaVersion int    `json:"schemaVersion"`
		Scope         struct {
			OperationID     string `json:"operationId"`
			ActionID        string `json:"actionId"`
			Actor           string `json:"actor"`
			RequestedPath   string `json:"requestedPath"`
			Snapshot        string `json:"snapshot"`
			WriterFence     int64  `json:"writerFence"`
			CutoverRevision int64  `json:"cutoverRevision"`
			Epoch           uint64 `json:"epoch"`
		} `json:"scope"`
		Marker        []byte                 `json:"marker"`
		Document      json.RawMessage        `json:"document"`
		Record        Record                 `json:"record"`
		Result        OperatorMutationResult `json:"result"`
		PreviousPath  string                 `json:"previousPath"`
		EventBoundary int64                  `json:"eventBoundary"`
	}
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Fatal(err)
	}
	if value.SourceCommit != legacyTargetV21Source || value.SchemaVersion != 21 || value.Record.Revision != 2 || value.EventBoundary < 2 {
		t.Fatal("producer source/schema/history identity did not match v21")
	}
	marker := &actionv1.FilesystemBackupTargetInspectionReceipt{}
	if err := proto.Unmarshal(value.Marker, marker); err != nil {
		t.Fatal(err)
	}
	if err := verifyTargetObservation(marker, promotionTestPublic, "fleet-a", "production"); err != nil {
		t.Fatal("retained marker fixture signature invalid", err)
	}
	scope := BackupTargetRegistrationScope{operationID: value.Scope.OperationID, actionID: value.Scope.ActionID, actor: value.Scope.Actor,
		requestedPath: value.Scope.RequestedPath, snapshot: value.Scope.Snapshot, writerFence: value.Scope.WriterFence,
		cutoverRevision: value.Scope.CutoverRevision, epoch: value.Scope.Epoch}
	return legacyTargetLocationFixture{path: filepath.Join(output, "go-control"), previous: value.PreviousPath, scope: scope, marker: marker,
		document: value.Document, record: value.Record, result: value.Result, boundary: value.EventBoundary}
}

func openLegacyTargetV21Copy(t *testing.T, fixture legacyTargetLocationFixture) *Control {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(fixture.path, ControlDatabaseFilename))
	if err != nil {
		t.Fatal(err)
	}
	directory := filepath.Join(t.TempDir(), "go-control")
	if err := os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(directory, ControlDatabaseFilename), data, 0600); err != nil {
		t.Fatal(err)
	}
	return reopenTargetDeleteStore(t, directory, func() int64 { return 1000 })
}
