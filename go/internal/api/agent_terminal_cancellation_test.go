package api

import (
	"context"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestTerminalAgentRPCDenialAndCancellationCannotCreateCompletion(t *testing.T) {
	control := agentExecutionRPCStore(t, true)
	before, err := control.ExportSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	fence := &commonv1.ActionFence{ActionId: "admitted-action", ExecutionEpoch: 1}
	for _, test := range []struct {
		name string
		call func(*agentNodeService, context.Context) error
	}{
		{"task", func(s *agentNodeService, ctx context.Context) error {
			_, err := s.SettleTask(ctx, &agentv1.SettleAgentTaskRequest{SchemaVersion: 1, Fence: fence})
			return err
		}},
		{"phase", func(s *agentNodeService, ctx context.Context) error {
			_, err := s.CompleteTasks(ctx, &agentv1.CompleteAgentTasksRequest{SchemaVersion: 1, Fence: fence})
			return err
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			shadow := &agentNodeService{run: &agentRunService{control: control, health: ShadowHealth()}}
			if err := test.call(shadow, context.Background()); status.Code(err) != codes.PermissionDenied {
				t.Fatalf("shadow terminal mutation admitted: %v", err)
			}
			missing := &agentNodeService{run: &agentRunService{health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}}
			if err := test.call(missing, context.Background()); status.Code(err) != codes.Unavailable {
				t.Fatalf("missing durable store admitted completion: %v", err)
			}
			service := &agentNodeService{run: &agentRunService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if err := test.call(service, ctx); status.Code(err) != codes.Canceled {
				t.Fatalf("canceled terminal write entered durable processing: %v", err)
			}
			ctx, cancel = context.WithDeadline(context.Background(), time.Unix(1, 0))
			defer cancel()
			if err := test.call(service, ctx); status.Code(err) != codes.DeadlineExceeded {
				t.Fatalf("expired terminal write entered durable processing: %v", err)
			}
		})
	}
	after, err := control.ExportSnapshot()
	if err != nil || after.Digest != before.Digest {
		t.Fatalf("terminal refusal changed a durable row: %v", err)
	}
}
