package api

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestAgentChatRPCRequiresTheRealInternalCredentialAndReportsItsActualMode(t *testing.T) {
	for _, mode := range []string{config.ModeShadow, config.ModeAuthoritative} {
		t.Run(mode, func(t *testing.T) {
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			rpc := NewControlRPC(nil, testInternalBearer, HealthFor(config.Config{Mode: mode}))
			done := make(chan error, 1)
			go func() { done <- rpc.Serve(listener) }()
			connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
			if err != nil {
				rpc.Stop()
				<-done
				t.Fatal(err)
			}
			t.Cleanup(func() { connection.Close(); rpc.Stop(); <-done })
			client := agentv1.NewAgentChatSchedulerClient(connection)
			input := &agentv1.AgentChatScheduleInput{Candidates: []*agentv1.AgentChatTask{{Role: "coder", Task: "code", DependsOn: []string{"researcher"}}, {Role: "researcher", Task: "research"}}}
			for _, bearer := range []string{"", "wrong", testInternalBearer} {
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				if bearer != "" {
					ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+bearer)
				}
				output, err := client.Evaluate(ctx, input)
				cancel()
				if bearer != testInternalBearer {
					if status.Code(err) != codes.Unauthenticated {
						t.Fatalf("unauthorized scheduling: %v", err)
					}
				} else if err != nil || output.Authoritative != (mode == config.ModeAuthoritative) || len(output.Execute) != 1 || output.Execute[0].Role != "researcher" {
					t.Fatalf("real schedule: %v %v", output, err)
				}
			}
			ctx := controlRPCContext(t, testInternalBearer)
			if _, err := client.Evaluate(ctx, &agentv1.AgentChatScheduleInput{Outcomes: []*agentv1.AgentChatOutcome{{Role: "unknown"}}}); status.Code(err) != codes.InvalidArgument {
				t.Fatalf("malformed schedule accepted: %v", err)
			}
		})
	}
}
