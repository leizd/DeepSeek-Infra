package lifecycle

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestRuntimeControlRPCSharesListenerAndClosesOnCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	bearer := "runtime-control-rpc-bearer-0123456789"
	runtime, err := Start(ctx, config.Config{Listen: "127.0.0.1:0", Mode: config.ModeShadow, InternalAPIBearer: bearer})
	if err != nil {
		t.Fatal(err)
	}
	connection, err := grpc.NewClient(runtime.Addr(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	client := controlv1.NewControlPlaneClient(connection)
	requestCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	if _, err := client.Health(requestCtx, &controlv1.HealthRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("unauthenticated RPC: %v", err)
	}
	authenticated := metadata.AppendToOutgoingContext(requestCtx, "authorization", "Bearer "+bearer)
	health, err := client.Health(authenticated, &controlv1.HealthRequest{})
	if err != nil || !health.Ok {
		t.Fatalf("authenticated RPC: %v %v", health, err)
	}
	response, err := http.Get("http://" + runtime.Addr() + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("HTTP listener changed: %d", response.StatusCode)
	}
	cancel()
	select {
	case err := <-runtime.Done():
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("RPC listener prevented shutdown")
	}
	if _, err := client.Health(authenticated, &controlv1.HealthRequest{}); err == nil {
		t.Fatal("RPC still admitted after shutdown")
	}
}
