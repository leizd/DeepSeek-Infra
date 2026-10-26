package api

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"net"
	"net/http"
	"testing"
	"time"
)

func TestMediaControlRPCLargeTypedClaimRetainsTheOriginalLegacyMessageLimit(t *testing.T) {
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-large-claim", LeaseSeconds: 3600})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { control.Close() })
	// This HTTP-valid object fits below the original one-million-byte limit,
	// but its typed tree legitimately needs more than the legacy RPC's1MiB.
	fields := make(map[string]any, 80000)
	for i := 0; i < 80000; i++ {
		fields[fmt.Sprintf("k%05d", i)] = map[string]any{}
	}
	body, err := json.Marshal(map[string]any{"metadata": fields})
	if err != nil || len(body) > 1_000_000 {
		t.Fatal("invalid size fixture", len(body), err)
	}
	value, err := mediamodel.FromJSON(fields)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitMediaPatch(&mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-patch-large-claim", MediaId: "media_one", Patch: &mediav1.MediaPatch{Metadata: value}})
	if err != nil {
		t.Fatal(err)
	}
	if size := proto.Size(claim); size <= 1<<20 || size > 2<<20 {
		t.Fatal("typed claim did not cross the required bounded range", size)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	rpc := NewControlRPC(control, testInternalBearer, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true)
	server := &http.Server{Handler: rpc, Protocols: protocols}
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close(); rpc.Stop(); server.Close(); <-done })
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+testInternalBearer)
	actual, err := mediav1.NewMediaMutationControlClient(connection).GetClaim(ctx, &mediav1.GetMediaPatchClaimRequest{SchemaVersion: 1, OperationId: claim.Intent.OperationId, Fence: claim.Fence})
	if err != nil || !proto.Equal(actual, claim) {
		t.Fatal("bounded typed media claim failed", err)
	}
	legacy := controlv1.NewControlPlaneClient(connection)
	if _, err := legacy.ShadowEvaluate(ctx, &controlv1.ShadowEvaluateRequest{CanonicalInput: make([]byte, (1<<20)+1)}); status.Code(err) != codes.ResourceExhausted {
		t.Fatal("legacy oversized payload reached evaluation", err)
	}
	if healthy, err := legacy.Health(ctx, &controlv1.HealthRequest{}); err != nil || !healthy.Ok {
		t.Fatal("rejection damaged original listener", err)
	}
}
