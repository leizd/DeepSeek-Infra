package worker

import (
	"context"
	"crypto/sha256"
	"errors"
	"strings"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

func TestMediaProcessClientRetainsOperationAndNeverRetriesTransportFailure(t *testing.T) {
	f := &mediaForwardingFixture{}
	c := &Client{tlsSecured: true, bearerAttached: true, media: f}
	r := &mediav1.ExecuteMediaProcessRequest{SchemaVersion: 1, OperationId: "media-process-" + strings.Repeat("a", 32), Fence: &commonv1.ActionFence{ExecutionEpoch: 1}}
	r.Fence.ActionId = r.OperationId
	o, err := c.ExecuteMediaProcess(context.Background(), r)
	if err != nil || o.OperationId != r.OperationId || !proto.Equal(f.process, r) || f.processOptions != 1 {
		t.Fatal("original process request or limits changed", err)
	}
	f.failure = errors.New("isolated process response loss")
	if _, err = c.ExecuteMediaProcess(context.Background(), r); !errors.Is(err, f.failure) || f.calls != 2 {
		t.Fatal("response loss was hidden or retried", err, f.calls)
	}
	ctx := metadata.NewOutgoingContext(context.Background(), metadata.Pairs("authorization", "Bearer conflicting"))
	if _, err = c.ExecuteMediaProcess(ctx, r); err == nil || f.calls != 2 {
		t.Fatal("conflicting credential forwarded", err)
	}
	for _, client := range []*Client{nil, {}, {tlsSecured: true, media: f}, {bearerAttached: true, media: f}, {tlsSecured: true, bearerAttached: true}} {
		if _, err = client.ExecuteMediaProcess(context.Background(), r); err == nil {
			t.Fatal("unsecured process client accepted")
		}
	}
}

func TestMediaProcessResultRequiresBoundRecordSegmentsCountAndOriginalFence(t *testing.T) {
	v, err := mediamodel.FromJSON(map[string]any{"mediaId": "media_one", "status": "ready"})
	if err != nil {
		t.Fatal(err)
	}
	response := &mediav1.ReadMediaCreateResultResponse{Media: v.GetObjectValue(), Segments: &mediav1.MediaArray{}, Indexed: 0}
	raw, _ := (proto.MarshalOptions{Deterministic: true}).Marshal(response.Media)
	recordHash := sha256.Sum256(raw)
	empty := sha256.Sum256(nil)
	fence := &commonv1.ActionFence{ActionId: "media-process-" + strings.Repeat("a", 32), ExecutionEpoch: 1}
	base := &mediav1.MediaPatchOutcome{OperationId: fence.ActionId, OriginalFence: fence, Processed: &mediav1.MediaProcessedResult{OriginalFence: proto.Clone(fence).(*commonv1.ActionFence), RecordBytes: uint64(len(raw)), RecordSha256: recordHash[:], SegmentsSha256: empty[:]}}
	for _, name := range []string{"valid-empty", "nil-outcome", "missing-binding", "mixed-create", "missing-fence", "foreign-fence", "wrong-operation", "record-hash", "record-size", "segment-hash", "segment-size", "missing-response", "missing-record", "missing-segments", "wrong-index-count", "unknown-response", "transport-loss", "nil-context", "conflicting-auth"} {
		t.Run(name, func(t *testing.T) {
			o := proto.Clone(base).(*mediav1.MediaPatchOutcome)
			r := proto.Clone(response).(*mediav1.ReadMediaCreateResultResponse)
			f := &mediaCreateClientFixture{response: r}
			c := &Client{tlsSecured: true, bearerAttached: true, mediaCreation: f}
			ctx := context.Background()
			switch name {
			case "nil-outcome":
				o = nil
			case "missing-binding":
				o.Processed = nil
			case "mixed-create":
				o.Created = &mediav1.MediaCreatedResult{}
			case "missing-fence":
				o.OriginalFence = nil
			case "foreign-fence":
				o.Processed.OriginalFence.ExecutionEpoch++
			case "wrong-operation":
				o.OperationId = "media-create-other"
			case "record-hash":
				o.Processed.RecordSha256 = empty[:]
			case "record-size":
				o.Processed.RecordBytes++
			case "segment-hash":
				o.Processed.SegmentsSha256 = recordHash[:]
			case "segment-size":
				o.Processed.SegmentsBytes++
			case "missing-response":
				f.response = nil
			case "missing-record":
				r.Media = nil
			case "missing-segments":
				r.Segments = nil
			case "wrong-index-count":
				r.Indexed++
			case "unknown-response":
				r.ProtoReflect().SetUnknown([]byte{0x20, 1})
			case "transport-loss":
				f.readFailure = errors.New("isolated result response loss")
			case "nil-context":
				ctx = nil
			case "conflicting-auth":
				ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("authorization", "Bearer conflicting"))
			}
			got, e := c.ReadMediaProcessResult(ctx, o)
			if name == "valid-empty" {
				if e != nil || got.Segments == nil || got.Indexed != 0 || f.read != 1 || !proto.Equal(f.request.OriginalFence, fence) {
					t.Fatal("valid empty result rejected or rebound", e)
				}
			} else if e == nil {
				t.Fatal("unbound processing result accepted")
			}
			if f.read > 1 || f.opened != 0 {
				t.Fatal("result was retried or staged as CREATE")
			}
		})
	}
}
