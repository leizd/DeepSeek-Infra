package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// This synthetic peer qualifies Go over actual pinned TLS/Protobuf. Rust
// custody, index/segment effects and recovery need their independent workloads.
type mediaProcessWirePeer struct {
	*mediaCreateWirePeer
	result *mediav1.ReadMediaCreateResultResponse
}

func (p *mediaProcessWirePeer) ExecuteProcess(_ context.Context, r *mediav1.ExecuteMediaProcessRequest) (*mediav1.MediaPatchOutcome, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.executed++
	claim, err := p.control.GetMediaPatchClaim(r.OperationId, r.Fence)
	if err != nil || r.SchemaVersion != 1 || claim.Intent.Kind != mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_PROCESS || claim.Intent.Process == nil || claim.Intent.Create != nil || p.stageCalls != 0 {
		return nil, status.Error(codes.FailedPrecondition, "original standalone process claim required")
	}
	preparation := bytes.Repeat([]byte{7}, 32)
	fields := map[string]any{"mediaId": claim.Intent.MediaId, "status": "ready", "title": "retained process result"}
	if p.mode == "large" {
		fields["metadata"] = map[string]any{"blob": strings.Repeat("x", 4*1024*1024+257)}
	}
	if p.mode == "processing-failure" {
		fields["status"] = "failed"
	}
	v, err := mediamodel.FromJSON(fields)
	if err != nil {
		return nil, err
	}
	array, err := mediamodel.FromJSON([]any{map[string]any{"segmentId": "segment_fixture", "text": "retained original result"}})
	if err != nil {
		return nil, err
	}
	p.result = &mediav1.ReadMediaCreateResultResponse{Media: v.GetObjectValue(), Segments: array.GetArrayValue(), Indexed: 1}
	if p.mode == "processing-failure" {
		p.result.Segments = &mediav1.MediaArray{}
		p.result.Indexed = 0
	}
	if p.mode == "result-invalid" {
		p.result.Media.Members[0].Value = &mediav1.MediaValue{}
	}
	record, err := (proto.MarshalOptions{Deterministic: true}).Marshal(p.result.Media)
	if err != nil {
		return nil, err
	}
	segments, err := (proto.MarshalOptions{Deterministic: true}).Marshal(p.result.Segments)
	if err != nil {
		return nil, err
	}
	recordHash := sha256.Sum256(record)
	segmentsHash := sha256.Sum256(segments)
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: r.OperationId, OriginalFence: proto.Clone(r.Fence).(*commonv1.ActionFence), RequestSha256: claim.RequestSha256,
		State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{1}, 32), AfterSha256: bytes.Repeat([]byte{2}, 32), FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken,
		Processed: &mediav1.MediaProcessedResult{RecordSha256: recordHash[:], RecordBytes: uint64(len(record)), PreparationSha256: preparation, OriginalFence: proto.Clone(r.Fence).(*commonv1.ActionFence), SegmentsSha256: segmentsHash[:], SegmentsBytes: uint64(len(segments)), Indexed: p.result.Indexed}}
	if p.mode == "refused" {
		outcome.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
		outcome.Processed = nil
		outcome.AfterSha256 = nil
		outcome.HttpStatus = 404
		outcome.ErrorCode = "not_found"
		outcome.ErrorMessage = "Media not found"
	}
	if p.mode == "processing-failure" {
		outcome.HttpStatus = 422
		outcome.ErrorCode = "invalid_payload"
		outcome.ErrorMessage = "Could not extract text from this PDF"
	}
	encoded, err := mediamodel.Encode(outcome)
	if err != nil {
		return nil, err
	}
	planDigest := sha256.Sum256(encoded)
	if _, err = p.control.BeginMediaPatchDispatch(&mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1, OperationId: r.OperationId, Fence: r.Fence, ClaimToken: claim.ClaimToken, WriterFencingToken: claim.WriterFencingToken, PreparationSha256: planDigest[:]}); err != nil {
		return nil, err
	}
	if p.mode == "unknown" {
		return nil, status.Error(codes.Unavailable, "isolated response loss without evidence")
	}
	if p.mode == "rebound-private-preparation" {
		outcome.Processed.PreparationSha256[0] ^= 1
	}
	if p.mode == "rebound-index-count" {
		outcome.Processed.Indexed++
	}
	encoded, err = mediamodel.Encode(outcome)
	if err != nil {
		return nil, err
	}
	outcome.Signature = ed25519.Sign(p.key, append([]byte("deepseek-infra:media-patch-outcome-v1\x00"), encoded...))
	if p.mode == "forged" {
		outcome.Signature[0] ^= 1
	}
	p.retained = proto.Clone(outcome).(*mediav1.MediaPatchOutcome)
	if p.mode == "lost" {
		return nil, status.Error(codes.Unavailable, "isolated response loss after retention")
	}
	return outcome, nil
}

func (p *mediaProcessWirePeer) ReadResult(_ context.Context, r *mediav1.ReadMediaCreateResultRequest) (*mediav1.ReadMediaCreateResultResponse, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.readCalls++
	if p.retained == nil || r.SchemaVersion != 1 || r.OperationId != p.retained.OperationId || !proto.Equal(r.OriginalFence, p.retained.OriginalFence) {
		return nil, status.Error(codes.FailedPrecondition, "original process result required")
	}
	if p.mode == "result-unavailable" {
		return nil, status.Error(codes.Unavailable, "isolated result response loss")
	}
	if p.mode == "result-missing" {
		return &mediav1.ReadMediaCreateResultResponse{}, nil
	}
	result := proto.Clone(p.result).(*mediav1.ReadMediaCreateResultResponse)
	if p.mode == "result-mismatch" {
		result.Indexed++
	}
	return result, nil
}

func mediaProcessTLSFixture(t *testing.T, mode string) (*store.Control, *mediaProcessWirePeer, *httptest.Server) {
	t.Helper()
	var peer *mediaProcessWirePeer
	control, _, server := mediaWireFixture(t, mode, func(rpc *grpc.Server, base *mediaWirePeer) {
		peer = &mediaProcessWirePeer{mediaCreateWirePeer: &mediaCreateWirePeer{mediaWirePeer: base}}
		mediav1.RegisterMediaMutationWorkerServer(rpc, peer)
		mediav1.RegisterMediaCreationPayloadsServer(rpc, peer)
	})
	server.Client().Timeout = 30 * time.Second
	return control, peer, server
}

func TestMediaPublicProcessTLSBindsResultsAndRecoversOnlyTheOriginalOperation(t *testing.T) {
	for _, mode := range []string{"success", "large", "lost", "unknown", "forged", "refused", "processing-failure", "service-auth-refused", "result-unavailable", "result-missing", "result-mismatch", "result-invalid", "rebound-private-preparation", "rebound-index-count"} {
		t.Run(mode, func(t *testing.T) {
			control, peer, server := mediaProcessTLSFixture(t, mode)
			if mode == "service-auth-refused" {
				t.Setenv(worker.EnvWorkerServiceBearer, strings.Repeat("w", 32))
			}
			r, err := http.NewRequest(http.MethodPost, server.URL+"/api/media/media_existing/process?force=off", strings.NewReader(`{"force":true,"ocrEnabled":false}`))
			if err != nil {
				t.Fatal(err)
			}
			r.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
			r.Header.Set("X-DeepSeek-Request-ID", "tls.process:original")
			response, err := server.Client().Do(r)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			var body map[string]any
			decoder := json.NewDecoder(response.Body)
			decoder.UseNumber()
			if err = decoder.Decode(&body); err != nil {
				t.Fatal(err)
			}
			op := response.Header.Get("X-DeepSeek-Operation-Id")
			if !strings.HasPrefix(op, "media-process-") || response.Header.Get("X-DeepSeek-Request-ID") != "tls.process:original" {
				t.Fatal("original operation/correlation missing", op)
			}
			claim, err := control.GetMediaPatchClaim(op, &commonv1.ActionFence{ActionId: op, ExecutionEpoch: 1})
			if err != nil {
				t.Fatal(err)
			}
			if claim.Intent.MediaId != "media_existing" || !claim.Intent.Process.Force || claim.Intent.Process.OcrEnabled == nil || *claim.Intent.Process.OcrEnabled || claim.Intent.Process.CorrelationId != "tls.process:original" || claim.Intent.Create != nil {
				t.Fatal("original media identity or typed process options changed", claim)
			}
			state, statusCode, queries, reads, executions := "APPLIED", 200, 0, 1, 1
			switch mode {
			case "lost":
				queries = 1
			case "unknown", "forged", "rebound-private-preparation", "rebound-index-count":
				state, statusCode, queries, reads = "DISPATCHED", 503, 1, 0
			case "service-auth-refused":
				state, statusCode, queries, reads, executions = "CLAIMED", 503, 0, 0, 0
			case "refused":
				state, statusCode, reads = "REFUSED", 404, 0
			case "processing-failure":
				statusCode = 422
			case "result-unavailable", "result-missing", "result-mismatch", "result-invalid":
				statusCode = 503
			}
			if claim.State != state || response.StatusCode != statusCode {
				t.Fatal("wrong durable/public process outcome", claim.State, response.StatusCode, body)
			}
			if statusCode == 200 {
				if body["ok"] != true || len(body["segments"].([]any)) != 1 || body["indexed"] != json.Number("1") {
					t.Fatal("bound public process result changed", body)
				}
			}
			if mode == "processing-failure" && (body["code"] != "invalid_payload" || body["error"] != "Could not extract text from this PDF") {
				t.Fatal("applied processing failure lost its original public error", body)
			}
			peer.mu.Lock()
			actualQueries, actualReads, actualExecutions, stages := peer.queried, peer.readCalls, peer.executed, peer.stageCalls
			peer.mu.Unlock()
			if actualQueries != queries || actualReads != reads || actualExecutions != executions || stages != 0 {
				t.Fatal("operation was retried, staged as CREATE, or result not checked", actualQueries, actualReads, actualExecutions, stages)
			}
			pending, err := control.PendingMediaPatch()
			if err != nil || (pending != nil) != (state == "CLAIMED" || state == "DISPATCHED") {
				t.Fatal("unknown effect was cleared or applied effect was left pending", err)
			}
			if mode == "unknown" {
				second, err := http.NewRequest(http.MethodPost, server.URL+"/api/media/media_existing/process", nil)
				if err != nil {
					t.Fatal(err)
				}
				second.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
				repeated, err := server.Client().Do(second)
				if err != nil {
					t.Fatal(err)
				}
				defer repeated.Body.Close()
				if repeated.StatusCode != 503 || repeated.Header.Get("X-DeepSeek-Operation-Id") != op {
					t.Fatal("pending original effect was replaced", repeated.StatusCode)
				}
				peer.mu.Lock()
				execs, queries := peer.executed, peer.queried
				peer.mu.Unlock()
				if execs != 1 || queries != 2 {
					t.Fatal("pending unknown effect was executed again")
				}
			}
		})
	}
}

func TestMediaPublicProcessSettlesPendingOriginalBeforeAnotherExplicitRequest(t *testing.T) {
	control, peer, server := mediaProcessTLSFixture(t, "success")
	i := &mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-process-" + strings.Repeat("a", 32), MediaId: "media_existing", Kind: mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_PROCESS, Process: &mediav1.MediaProcessOptions{Force: true}}
	claim, err := control.AdmitMediaPatch(i)
	if err != nil {
		t.Fatal(err)
	}
	client, err := dialMediaWorker()
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if _, err = client.ExecuteMediaProcess(context.Background(), &mediav1.ExecuteMediaProcessRequest{SchemaVersion: 1, OperationId: i.OperationId, Fence: claim.Fence}); err != nil {
		t.Fatal(err)
	}
	// The original reply has not been reconciled into Go. This is another
	// explicit request, not an automatic retry of the original operation.
	r, err := http.NewRequest(http.MethodPost, server.URL+"/api/media/media_existing/process", nil)
	if err != nil {
		t.Fatal(err)
	}
	r.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
	response, err := server.Client().Do(r)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != 200 || response.Header.Get("X-DeepSeek-Operation-Id") == i.OperationId {
		t.Fatal("original was not settled before the new explicit request", response.StatusCode)
	}
	original, err := control.GetMediaPatchClaim(i.OperationId, claim.Fence)
	if err != nil || original.State != "APPLIED" {
		t.Fatal("original outcome missing", err)
	}
	peer.mu.Lock()
	defer peer.mu.Unlock()
	if peer.executed != 2 || peer.queried != 1 || peer.stageCalls != 0 || peer.readCalls != 1 {
		t.Fatal("unexpected dispatch/recovery sequence", peer.executed, peer.queried, peer.readCalls)
	}
}
