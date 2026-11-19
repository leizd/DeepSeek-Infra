package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	targetmodel "github.com/leizd/DeepSeek-Infra/go/internal/targets"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

var publicS3FixturePlacement = targetmodel.S3Placement{Endpoint: "https://s3-fixture.test", Region: "us-east-1", Bucket: "fixture-bucket", Prefix: "retained/nested"}

// The HTTP and TLS paths are real. This peer supplies fixture signatures, not
// provider observations or evidence of Rust custody.
func (p *publicTargetPeer) InspectS3(ctx context.Context, request *actionv1.InspectS3BackupTargetRequest) (*actionv1.S3BackupTargetInspectionReceipt, error) {
	p.calls.Add(1)
	authorization, _ := metadata.FromIncomingContext(ctx)
	if values := authorization.Get("authorization"); len(values) != 1 || values[0] != "Bearer "+strings.Repeat("t", 32) {
		return nil, status.Error(codes.Unauthenticated, "isolated controller credential required")
	}
	if !bytes.Equal(request.TargetIdentity, publicS3FixturePlacement.Fingerprint()) {
		return nil, status.Error(codes.PermissionDenied, "isolated provider placement mismatch")
	}
	observed := &actionv1.S3BackupTargetObservation{
		SchemaVersion: 1, Fence: request.Fence, OperationId: request.OperationId, WriterFencingToken: request.WriterFencingToken,
		TargetIdentity: request.TargetIdentity, Bucket: publicS3FixturePlacement.Bucket, Prefix: publicS3FixturePlacement.Prefix,
		ObservedAtUnix: time.Now().Unix(), FleetId: p.binding.FleetID, Environment: p.binding.Environment,
	}
	if p.mode != "s3-absent" {
		observed.Identity = publicS3Object(`{"schemaVersion":3,"kind":"s3","targetId":"target_s3_public","bucket":"fixture-bucket","prefix":"retained/nested","incarnationId":"inc_fixture","createdAt":"2026-11-19T00:00:00Z"}`)
		observed.Head = publicS3Object(`{"schemaVersion":1,"targetGeneration":0,"latestCommitHash":"` + strings.Repeat("0", 64) + `","incarnationId":"inc_fixture"}`)
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(observed)
	if err != nil {
		return nil, status.Error(codes.Internal, "isolated observation encoding failed")
	}
	return &actionv1.S3BackupTargetInspectionReceipt{SchemaVersion: 1, SignerPublicKey: p.binding.PublicKey, Observation: observed,
		Signature: ed25519.Sign(promotionRoutePrivate, append([]byte("deepseek-infra:backup-target-s3-observation-v1\x00"), wire...))}, nil
}

func publicS3Object(raw string) *actionv1.S3BackupTargetObjectObservation {
	hash := sha256.Sum256([]byte(raw))
	return &actionv1.S3BackupTargetObjectObservation{Json: []byte(raw), Sha256: hash[:], Etag: `"provider-etag"`, VersionId: "opaque-version"}
}

const publicS3Registration = `{"kind":"s3-compatible","bucket":" fixture-bucket ","prefix":"/retained/nested/","endpointUrl":"https://s3-fixture.test/","label":"retained namespace","quotaBytes":9007199254740993,"probe":false}`
const targetS3PublicRoute = "/api/workspace/backup-targets"

func configurePublicS3Test(t *testing.T) {
	t.Helper()
	t.Setenv("DEEPSEEK_WORKER_S3_ENDPOINT", publicS3FixturePlacement.Endpoint)
	t.Setenv("DEEPSEEK_WORKER_S3_REGION", publicS3FixturePlacement.Region)
	t.Setenv("DEEPSEEK_WORKER_S3_BUCKET", publicS3FixturePlacement.Bucket)
	t.Setenv("DEEPSEEK_WORKER_S3_PREFIX", publicS3FixturePlacement.Prefix)
}

func TestTargetRegistrationPublicS3PersistsAndReplaysWithoutAnotherProviderRead(t *testing.T) {
	configurePublicS3Test(t)
	control, peer, httpServer, tls := publicTargetTLSFixture(t, "s3-present")
	code, original, operation := publicTargetHTTPRequest(t, httpServer, http.MethodPost, targetS3PublicRoute, publicS3Registration, "")
	if code != http.StatusOK || operation == "" || peer.calls.Load() != 1 {
		t.Fatalf("public registration: %d %s %s", code, operation, original)
	}
	var result map[string]json.RawMessage
	if err := json.Unmarshal(original, &result); err != nil {
		t.Fatal(err)
	}
	for key, expected := range map[string]string{
		"targetId": `"target_s3_public"`, "schemaVersion": "3", "kind": `"s3"`, "label": `"retained namespace"`,
		"bucket": `"fixture-bucket"`, "prefix": `"retained/nested"`, "region": "null", "endpointUrl": `"https://s3-fixture.test/"`,
		"quotaBytes": "9007199254740993", "lastProbe": "null", "topologyGeneration": "1", "failureDomain": `"remote-s3"`,
		"credentialProvider": `{"type":"aws-default-chain"}`, "createdAt": `"2026-11-19T00:00:00Z"`,
	} {
		if string(result[key]) != expected {
			t.Fatalf("%s: %s want %s", key, result[key], expected)
		}
	}
	tls.Close()
	code, replay, _ := publicTargetHTTPRequest(t, httpServer, http.MethodPost, targetS3PublicRoute, publicS3Registration, operation)
	if code != http.StatusOK || !bytes.Equal(original, replay) || peer.calls.Load() != 1 {
		t.Fatalf("public replay: %d %s", code, replay)
	}
	code, conflict, _ := publicTargetHTTPRequest(t, httpServer, http.MethodPost, targetS3PublicRoute, publicS3Registration+" ", operation)
	if code != http.StatusConflict || !bytes.Contains(conflict, []byte("mutation_request_replay_conflict")) {
		t.Fatalf("changed original bytes were accepted: %d %s", code, conflict)
	}
	count, err := control.OperatorJournalSize()
	if err != nil || count != 1 || peer.calls.Load() != 1 {
		t.Fatalf("replay added a target operation or read: %d %v", count, err)
	}
}

func TestTargetRegistrationPublicS3RejectsUnsupportedOrUnobservedLayout(t *testing.T) {
	configurePublicS3Test(t)
	control, peer, httpServer, _ := publicTargetTLSFixture(t, "s3-absent")
	requests := []struct {
		body, text string
		code       int
	}{
		{strings.Replace(publicS3Registration, `"probe":false`, `"probe":true`, 1), "BACKUP_TARGET_REGISTRATION_UNAVAILABLE", 503},
		{strings.Replace(publicS3Registration, "s3-fixture.test", "foreign-fixture.test", 1), "BACKUP_TARGET_REGISTRATION_UNAVAILABLE", 503},
		{strings.Replace(publicS3Registration, "9007199254740993", `"bad"`, 1), "quotaBytes", 400},
		{strings.Replace(publicS3Registration, `"probe":false`, `"probe":false,"credentialProvider":{"password":true}`, 1), "cloud credentials", 400},
		{publicS3Registration, "S3_BACKUP_TARGET_LAYOUT_INCOMPLETE", 503},
	}
	for _, request := range requests {
		code, body, _ := publicTargetHTTPRequest(t, httpServer, http.MethodPost, targetS3PublicRoute, request.body, "")
		if code != request.code || !strings.Contains(string(body), request.text) {
			t.Fatalf("unexpected result: %d %s, want %d %s", code, body, request.code, request.text)
		}
	}
	t.Setenv(worker.EnvWorkerTarget, "")
	code, body, _ := publicTargetHTTPRequest(t, httpServer, http.MethodPost, targetS3PublicRoute, publicS3Registration, store.PublicTargetOperationPrefix+"no-tls")
	if code != http.StatusServiceUnavailable || !strings.Contains(string(body), "BACKUP_TARGET_INSPECTION_UNAVAILABLE") {
		t.Fatalf("invalid loopback worker config did not fail closed: %d %s", code, body)
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 0 || peer.calls.Load() != 1 {
		t.Fatalf("invalid input reached mutation or provider: %d %d %v", count, peer.calls.Load(), err)
	}
}
