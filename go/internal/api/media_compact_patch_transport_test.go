package api

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"net/http"
	"strings"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// A Go-only TLS transport peer. It proves controller/HTTP behavior; only the
// separate actual native launcher workload can qualify Rust custody/effects.
type compactPatchWirePeer struct {
	*mediaWirePeer
	record []byte
	reads  int
}

func (p *compactPatchWirePeer) ExecutePatch(_ context.Context, r *mediav1.ExecuteMediaPatchRequest) (*mediav1.MediaPatchOutcome, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.executed++
	claim, err := p.control.GetMediaPatchClaim(r.OperationId, r.Fence)
	if err != nil {
		return nil, err
	}
	value, err := mediamodel.FromJSON(map[string]any{"mediaId": claim.Intent.MediaId, "title": "Changed", "metadata": map[string]any{"retained": strings.Repeat("x", 5*1024*1024)}})
	if err != nil {
		return nil, err
	}
	var record proto.Message = value.GetObjectValue()
	version, preparationVersion := uint32(2), uint32(1)
	domain := []byte("deepseek-infra:media-patch-outcome-v2\x00")
	if strings.HasPrefix(p.mode, "flat-") {
		version, preparationVersion = 4, 2
		domain = []byte("deepseek-infra:media-patch-outcome-v4\x00")
		record = &mediav1.MediaFlatPatchRecord{Nodes: []*mediav1.MediaFlatPatchNode{
			{Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: claim.Intent.MediaId}},
			{Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: "Changed"}},
			{Kind: &mediav1.MediaFlatPatchNode_TextValue{TextValue: strings.Repeat("x", 5*1024*1024)}},
			{Kind: &mediav1.MediaFlatPatchNode_ObjectValue{ObjectValue: &mediav1.MediaFlatPatchObject{Members: []*mediav1.MediaFlatPatchMember{{Key: "retained", Child: 3}}}}},
			{Kind: &mediav1.MediaFlatPatchNode_ObjectValue{ObjectValue: &mediav1.MediaFlatPatchObject{Members: []*mediav1.MediaFlatPatchMember{
				{Key: "mediaId", Child: 1}, {Key: "title", Child: 2}, {Key: "metadata", Child: 4},
			}}}},
		}}
	}
	if strings.HasSuffix(p.mode, "-invalid-record") {
		record.ProtoReflect().SetUnknown([]byte{0xf8, 7, 1})
	}
	p.record, err = (proto.MarshalOptions{Deterministic: true}).Marshal(record)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(p.record)
	preparation, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&mediav1.MediaPatchPreparation{SchemaVersion: preparationVersion, Claim: claim, RecordSha256: digest[:], RecordBytes: uint64(len(p.record))})
	if err != nil {
		return nil, err
	}
	prepared := sha256.Sum256(preparation)
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: version, OperationId: r.OperationId, OriginalFence: r.Fence, RequestSha256: claim.RequestSha256, State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED, HttpStatus: 200, BeforeSha256: digest[:], AfterSha256: digest[:], FleetId: claim.FleetId, Environment: claim.Environment, WriterFencingToken: claim.WriterFencingToken, Patched: &mediav1.MediaPatchedResult{RecordSha256: digest[:], RecordBytes: uint64(len(p.record)), PreparationSha256: prepared[:], OriginalFence: r.Fence}}
	encoded, err := mediamodel.Encode(outcome)
	if err != nil {
		return nil, err
	}
	dispatch := sha256.Sum256(encoded)
	if _, err = p.control.BeginMediaPatchDispatch(&mediav1.BeginMediaPatchDispatchRequest{SchemaVersion: 1, OperationId: r.OperationId, Fence: r.Fence, ClaimToken: claim.ClaimToken, WriterFencingToken: claim.WriterFencingToken, PreparationSha256: dispatch[:]}); err != nil {
		return nil, err
	}
	outcome.Signature = ed25519.Sign(p.key, append(domain, encoded...))
	p.retained = outcome
	if strings.HasSuffix(p.mode, "-lost-reply") {
		return nil, status.Error(codes.Unavailable, "isolated execution reply loss")
	}
	return proto.Clone(outcome).(*mediav1.MediaPatchOutcome), nil
}

func (p *compactPatchWirePeer) ReadPatchResult(r *mediav1.ReadMediaPatchResultRequest, s grpc.ServerStreamingServer[mediav1.MediaPatchResultChunk]) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.reads++
	claim, err := p.control.GetMediaPatchClaim(r.OperationId, r.OriginalFence)
	if err != nil {
		return err
	}
	if claim.State != "APPLIED" || p.retained == nil {
		return status.Error(codes.FailedPrecondition, "receipt not settled before read")
	}
	if err := s.Send(&mediav1.MediaPatchResultChunk{SchemaVersion: 1, Manifest: p.retained.Patched}); err != nil {
		return err
	}
	for offset := 0; offset < len(p.record); offset += 256 * 1024 {
		end := offset + 256*1024
		if end > len(p.record) {
			end = len(p.record)
		}
		if err := s.Send(&mediav1.MediaPatchResultChunk{SchemaVersion: 1, Offset: uint64(offset), RecordChunk: p.record[offset:end]}); err != nil {
			return err
		}
		if strings.HasSuffix(p.mode, "-interrupted-read") && p.reads == 1 {
			return status.Error(codes.Unavailable, "isolated result stream loss")
		}
	}
	return nil
}

func TestCompactPatchHTTPUsesTheOriginalAppliedRecordAfterFreshAndLostReplies(t *testing.T) {
	testPatchHTTPOriginalResult(t, "compact")
}

func TestFlatPatchHTTPUsesTheOriginalAppliedRecordAfterFreshAndLostReplies(t *testing.T) {
	testPatchHTTPOriginalResult(t, "flat")
}

func testPatchHTTPOriginalResult(t *testing.T, codec string) {
	t.Helper()
	for _, ending := range []string{"success", "lost-reply", "interrupted-read", "invalid-record"} {
		mode := codec + "-" + ending
		t.Run(mode, func(t *testing.T) {
			var compact *compactPatchWirePeer
			control, base, server := mediaWireFixture(t, mode, func(s *grpc.Server, p *mediaWirePeer) {
				compact = &compactPatchWirePeer{mediaWirePeer: p}
				mediav1.RegisterMediaMutationWorkerServer(s, compact)
			})
			code, body, operation := mediaWirePatch(t, server)
			success := strings.HasSuffix(mode, "-success") || strings.HasSuffix(mode, "-lost-reply")
			if success && code != http.StatusOK || !success && code != http.StatusServiceUnavailable {
				t.Fatalf("HTTP=%d mode=%s", code, mode)
			}
			if success {
				record := body["media"].(map[string]any)
				meta := record["metadata"].(map[string]any)
				if record["title"] != "Changed" || meta["retained"] != strings.Repeat("x", 5*1024*1024) {
					t.Fatal("public retained record differs")
				}
			}
			base.mu.Lock()
			outcome := proto.Clone(base.retained).(*mediav1.MediaPatchOutcome)
			executed, queried := base.executed, base.queried
			base.mu.Unlock()
			if operation != outcome.OperationId || executed != 1 || strings.HasSuffix(mode, "-lost-reply") && queried != 1 {
				t.Fatal("original operation repeated or rebound")
			}
			claim, err := control.GetMediaPatchClaim(operation, outcome.OriginalFence)
			if err != nil || claim.State != "APPLIED" {
				t.Fatal("read failure changed terminal effect", err)
			}
			if strings.HasSuffix(mode, "-interrupted-read") {
				client, err := dialMediaWorker()
				if err != nil {
					t.Fatal(err)
				}
				defer client.Close()
				if strings.HasPrefix(mode, "flat-") {
					_, err = client.ReadFlatMediaPatchResult(context.Background(), outcome)
				} else {
					_, err = client.ReadMediaPatchResult(context.Background(), outcome)
				}
				if err != nil {
					t.Fatal("original result could not be reread", err)
				}
				base.mu.Lock()
				defer base.mu.Unlock()
				if base.executed != 1 || compact.reads != 2 {
					t.Fatal("reread executed mutation")
				}
			}
		})
	}
}
