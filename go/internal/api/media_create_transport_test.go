package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
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

// This peer is a Go-only test oracle over actual pinned TLS/Protobuf. Its test
// key and synthetic result do not qualify Rust custody or media file effects.
type mediaCreateWirePeer struct {
	*mediaWirePeer
	mediav1.UnimplementedMediaCreationPayloadsServer
	staged                        []byte
	record                        *mediav1.MediaObject
	stageCalls, readCalls, chunks int
	uploadOperations              []string
}

func (p *mediaCreateWirePeer) Stage(stream grpc.ClientStreamingServer[mediav1.MediaCreateStageRequest, mediav1.MediaCreateStageResponse]) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.stageCalls++
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	claim, err := p.control.GetMediaPatchClaim(first.OperationId, first.Fence)
	if err != nil || first.SchemaVersion != 1 || first.GetManifest() == nil || claim.Intent.Kind != mediav1.MediaMutationKind_MEDIA_MUTATION_KIND_CREATE || !proto.Equal(first.GetManifest(), claim.Intent.Create) {
		return status.Error(codes.FailedPrecondition, "original creation manifest required")
	}
	var payload []byte
	for {
		message, err := stream.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		chunk := message.GetChunk()
		if message.SchemaVersion != 1 || message.OperationId != first.OperationId || !proto.Equal(message.Fence, first.Fence) || len(chunk) == 0 || len(chunk) > mediamodel.CreateChunkBytes || len(payload)+len(chunk) > mediamodel.MaxCreatePayloadBytes {
			return status.Error(codes.InvalidArgument, "chunk changed the original binding or bound")
		}
		p.chunks++
		payload = append(payload, chunk...)
	}
	hash := sha256.Sum256(payload)
	if uint64(len(payload)) != claim.Intent.Create.PayloadBytes || !bytes.Equal(hash[:], claim.Intent.Create.PayloadSha256) {
		return status.Error(codes.InvalidArgument, "content differs from admitted manifest")
	}
	p.staged = append([]byte(nil), payload...)
	if p.mode == "stage-rejected" {
		return status.Error(codes.Unavailable, "isolated stage response failure")
	}
	manifest := proto.Clone(first.GetManifest()).(*mediav1.MediaCreateManifest)
	if p.mode == "stage-manifest-mismatch" {
		manifest.PayloadBytes++
	}
	return stream.SendAndClose(&mediav1.MediaCreateStageResponse{Manifest: manifest})
}

func (p *mediaCreateWirePeer) ExecuteCreate(_ context.Context, r *mediav1.ExecuteMediaPatchRequest) (*mediav1.MediaPatchOutcome, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.executed++
	claim, err := p.control.GetMediaPatchClaim(r.OperationId, r.Fence)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, "original claim required")
	}
	stagesValid := p.stageCalls == 1
	if strings.HasPrefix(p.mode, "upload") {
		stagesValid = p.stageCalls == p.executed
	}
	if !stagesValid || len(p.staged) == 0 || claim.Intent.Create == nil {
		return nil, status.Error(codes.FailedPrecondition, "original content was not staged")
	}
	preparation := bytes.Repeat([]byte{7}, 32)
	_, err = p.control.BeginMediaPatchDispatch(&mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1, OperationId: r.OperationId, Fence: r.Fence, ClaimToken: claim.ClaimToken, WriterFencingToken: claim.WriterFencingToken, PreparationSha256: preparation})
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	if p.mode == "unknown" {
		return nil, status.Error(codes.Unavailable, "isolated loss without conclusive evidence")
	}
	var payload mediav1.MediaCreatePayload
	if err = proto.Unmarshal(p.staged, &payload); err != nil {
		return nil, err
	}
	var fields map[string]any
	if strings.HasPrefix(p.mode, "upload") {
		if payload.Upload == nil || payload.Registration != nil {
			return nil, status.Error(codes.InvalidArgument, "one typed upload required")
		}
		p.uploadOperations = append(p.uploadOperations, r.OperationId)
		hash := sha256.Sum256(payload.Upload.Data)
		fields = map[string]any{"title": payload.Upload.Title, "filename": payload.Upload.Filename,
			"mimeType": payload.Upload.ContentType, "projectId": payload.Upload.ProjectId, "dataSha256": hex.EncodeToString(hash[:])}
	} else {
		registration, parseErr := mediamodel.ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: payload.Registration}})
		if parseErr != nil {
			return nil, parseErr
		}
		var ok bool
		fields, ok = registration.(map[string]any)
		if !ok {
			return nil, status.Error(codes.InvalidArgument, "typed registration required")
		}
	}
	fields["mediaId"] = claim.Intent.MediaId
	value, err := mediamodel.FromJSON(fields)
	if err != nil {
		return nil, err
	}
	p.record = value.GetObjectValue()
	if p.mode == "result-invalid" {
		p.record.Members[0].Value = &mediav1.MediaValue{}
	}
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(p.record)
	if err != nil {
		return nil, err
	}
	hash := sha256.Sum256(encoded)
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: 1, OperationId: r.OperationId, OriginalFence: proto.Clone(r.Fence).(*commonv1.ActionFence), RequestSha256: claim.RequestSha256,
		State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, HttpStatus: 200, BeforeSha256: bytes.Repeat([]byte{1}, 32), AfterSha256: bytes.Repeat([]byte{2}, 32),
		FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken,
		Created: &mediav1.MediaCreatedResult{RecordSha256: hash[:], RecordBytes: uint64(len(encoded)), PreparationSha256: preparation, OriginalFence: proto.Clone(r.Fence).(*commonv1.ActionFence)}}
	if p.mode == "refused" || p.mode == "upload-refused" {
		outcome.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
		outcome.Created = nil
		outcome.AfterSha256 = nil
		outcome.HttpStatus = 400
		outcome.ErrorCode = "invalid_payload"
		outcome.ErrorMessage = "Isolated registration refusal"
	}
	canonical, err := mediamodel.Encode(outcome)
	if err != nil {
		return nil, err
	}
	outcome.Signature = ed25519.Sign(p.key, append([]byte("deepseek-infra:media-patch-outcome-v1\x00"), canonical...))
	p.retained = proto.Clone(outcome).(*mediav1.MediaPatchOutcome)
	if p.mode == "lost" {
		return nil, status.Error(codes.Unavailable, "isolated reply loss after retention")
	}
	return outcome, nil
}

func (p *mediaCreateWirePeer) ReadResult(_ context.Context, r *mediav1.ReadMediaCreateResultRequest) (*mediav1.ReadMediaCreateResultResponse, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.readCalls++
	if p.retained == nil || r.SchemaVersion != 1 || r.OperationId != p.retained.OperationId || !proto.Equal(r.OriginalFence, p.retained.OriginalFence) {
		return nil, status.Error(codes.FailedPrecondition, "read rebound original operation")
	}
	if p.mode == "result-unavailable" || p.mode == "upload-result-unavailable" {
		return nil, status.Error(codes.Unavailable, "isolated result read failure")
	}
	if p.mode == "result-missing" {
		return &mediav1.ReadMediaCreateResultResponse{}, nil
	}
	if p.mode == "result-mismatch" {
		return &mediav1.ReadMediaCreateResultResponse{Media: &mediav1.MediaObject{}}, nil
	}
	return &mediav1.ReadMediaCreateResultResponse{Media: proto.Clone(p.record).(*mediav1.MediaObject)}, nil
}

func mediaCreateTLSFixture(t *testing.T, mode string) (*store.Control, *mediaCreateWirePeer, *httptest.Server) {
	t.Helper()
	var creation *mediaCreateWirePeer
	control, _, server := mediaWireFixture(t, mode, func(rpc *grpc.Server, base *mediaWirePeer) {
		creation = &mediaCreateWirePeer{mediaWirePeer: base}
		mediav1.RegisterMediaMutationWorkerServer(rpc, creation)
		mediav1.RegisterMediaCreationPayloadsServer(rpc, creation)
	})
	server.Client().Timeout = 30 * time.Second
	return control, creation, server
}

func mediaCreateTLSRequest(t *testing.T, server *httptest.Server, payload []byte) (int, map[string]any, string) {
	t.Helper()
	r, err := http.NewRequest(http.MethodPost, server.URL+"/api/media", bytes.NewReader(payload))
	if err != nil {
		t.Fatal(err)
	}
	r.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
	r.Header.Set("Content-Type", "application/json")
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
	return response.StatusCode, body, response.Header.Get("X-DeepSeek-Operation-Id")
}

func TestMediaPublicCreateTLS(t *testing.T) {
	for _, mode := range []string{"success", "large", "lost", "unknown", "refused", "forged", "service-auth-refused", "stage-rejected", "stage-manifest-mismatch", "result-unavailable", "result-missing", "result-mismatch", "result-invalid"} {
		t.Run(mode, func(t *testing.T) {
			control, peer, server := mediaCreateTLSFixture(t, mode)
			if mode == "service-auth-refused" {
				t.Setenv(worker.EnvWorkerServiceBearer, strings.Repeat("w", 32))
			}
			blob := "retained"
			if mode == "large" {
				blob = strings.Repeat("x", 4*1024*1024+257)
			}
			payload, err := json.Marshal(map[string]any{"type": "image", "title": "Created", "mimeType": "image/png", "metadata": map[string]any{"flag": false, "zero": 0, "blob": blob}})
			if err != nil {
				t.Fatal(err)
			}
			code, body, operation := mediaCreateTLSRequest(t, server, payload)
			if operation == "" {
				t.Fatal("original operation header missing", code, body["code"])
			}
			claim, err := control.GetMediaPatchClaim(operation, &commonv1.ActionFence{ActionId: operation, ExecutionEpoch: 1})
			if err != nil {
				t.Fatal(err)
			}
			state, queries, reads, executions := "APPLIED", 0, 1, 1
			success := mode == "success" || mode == "large" || mode == "lost"
			if mode == "lost" {
				queries = 1
			}
			if mode == "unknown" || mode == "forged" {
				state, queries, reads = "DISPATCHED", 1, 0
			}
			if mode == "refused" {
				state, reads = "REFUSED", 0
			}
			if strings.HasPrefix(mode, "stage-") {
				state, queries, reads, executions = "CLAIMED", 1, 0, 0
			}
			stages := 1
			if mode == "service-auth-refused" {
				state, queries, reads, executions, stages = "CLAIMED", 0, 0, 0, 0
			}
			if claim.State != state {
				t.Fatal("original state differs", claim.State, state, code, body["code"])
			}
			if success {
				if code != 200 || body["ok"] != true {
					t.Fatal("legal creation failed", code, body["code"])
				}
				record, ok := body["media"].(map[string]any)
				if !ok || record["mediaId"] != claim.Intent.MediaId || record["title"] != "Created" {
					t.Fatal("public creation record differs")
				}
				metadata, ok := record["metadata"].(map[string]any)
				if !ok || metadata["flag"] != false || metadata["zero"] != json.Number("0") || metadata["blob"] != blob {
					t.Fatal("typed public metadata differs")
				}
			} else if mode == "refused" {
				if code != 400 || body["code"] != "invalid_payload" {
					t.Fatal("signed refusal changed", code, body["code"])
				}
			} else if code != 503 || body["code"] != "EFFECT_UNKNOWN" {
				t.Fatal("uncertain or invalid result became success", code, body["code"])
			}
			if mode == "unknown" {
				second, body, original := mediaCreateTLSRequest(t, server, payload)
				if second != 503 || body["code"] != "EFFECT_UNKNOWN" || original != operation {
					t.Fatal("new request rebound unresolved original")
				}
				queries++
			}
			peer.mu.Lock()
			defer peer.mu.Unlock()
			if peer.stageCalls != stages || peer.executed != executions || peer.queried != queries || peer.readCalls != reads {
				t.Fatal("unexpected stage/dispatch/query/read counts", peer.stageCalls, peer.executed, peer.queried, peer.readCalls)
			}
			if mode == "large" && (peer.chunks < 2 || peer.retained.Created.RecordBytes <= 4*1024*1024 || proto.Size(peer.retained) >= mediamodel.MaxMessageBytes) {
				t.Fatal("large record was coupled to compact signed control row")
			}
		})
	}
}
