package api

import (
	"context"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestRecoveryProgressDriverRequiresCurrentAuthoritativeControlBeforeWorkerRPC(t *testing.T) {
	for _, boundary := range []string{"unavailable-control", "shadow", "cursor"} {
		t.Run(boundary, func(t *testing.T) {
			driver, fixture, run := driverProgressFixtureRun(t, "critic", false)
			if err := driver.drive(context.Background(), run); err != nil {
				t.Fatal(err)
			}
			run = driverFixtureRun(t, driver, run.RunID)
			calls := len(fixture.calls)
			switch boundary {
			case "unavailable-control":
				if err := driver.service.control.Close(); err != nil {
					t.Fatal(err)
				}
			case "shadow":
				driver.service.health = HealthFor(config.Config{Mode: config.ModeShadow})
			case "cursor":
				run.NextIndex++
			}
			if err := driver.recoveryProgress(context.Background(), fixture, run); err == nil {
				t.Fatal("unverified control authorized progress", boundary)
			}
			if fixture.progressCalls != 0 || len(fixture.calls) != calls {
				t.Fatal("failed preview reached the worker or repeated a provider phase")
			}
		})
	}
}

func TestProgressServiceChecksItsOwnFenceAndStoreEvenWithoutTheTransportInterceptor(t *testing.T) {
	driver, fixture, _, _, valid := progressRPCFixture(t)
	service := &agentRunRecoveryService{run: driver.service}
	before := driverFixtureRun(t, driver, valid.RunId)
	for _, boundary := range []string{"fence", "cursor", "missing-run"} {
		t.Run(boundary, func(t *testing.T) {
			input := proto.Clone(valid).(*agentv1.PreviewAgentRecoveryProgressRequest)
			switch boundary {
			case "fence":
				input.MetadataFence.ExecutionEpoch++
			case "cursor":
				input.ExpectedIndex++
			case "missing-run":
				input.RunId = "run_missing_progress_guard"
				input.MetadataFence.ActionId = input.RunId
			}
			response, err := service.PreviewProgress(context.Background(), input)
			if err == nil || response != nil {
				t.Fatal("unverified direct preview succeeded", boundary)
			}
			if boundary == "fence" && status.Code(err) != codes.FailedPrecondition {
				t.Fatal("direct service lost its independent fence check", err)
			}
		})
	}
	after := driverFixtureRun(t, driver, valid.RunId)
	if after.NextIndex != before.NextIndex || after.RecoveryProgress != nil || fixture.progressCalls != 0 {
		t.Fatal("direct preview changed authority or called custody")
	}
}
