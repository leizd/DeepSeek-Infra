package api

import (
	"context"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

type agentRecoveryProgressWorker interface {
	PrepareAgentRecoveryProgress(context.Context, *agentv1.PrepareAgentRecoveryProgressRequest) (*agentv1.AgentRecoveryProgressReceipt, error)
}

func driverNeedsRecoveryProgress(run agent.RunMetadata) bool {
	if run.Recovery == nil || run.Recovery.Metadata == nil || run.RecoveryProgress != nil || run.CancellationActive() || run.Status != "running" {
		return false
	}
	context := run.Recovery.Metadata
	return context.Selection.GetKind() == agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RERUN &&
		!context.CompleteImmediately && len(context.Execute) == 1 && context.Execute[0] != nil && context.Execute[0].Id == context.Selection.AgentId && driverTasksComplete(run)
}

func (driver *agentRunDriver) recoveryProgress(ctx context.Context, client agentDriverWorker, run agent.RunMetadata) error {
	worker, ok := client.(agentRecoveryProgressWorker)
	if !ok {
		return errAgentDriverCustody
	}
	authority, err := driver.service.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		return err
	}
	if !authority.Authoritative {
		return errAgentDriverCustody
	}
	expected := store.AgentRecoveryProgressExpected{RunID: run.RunID, Fence: &commonv1.ActionFence{ActionId: run.RunID, ExecutionEpoch: authority.MetadataEpoch},
		ExpectedIndex: run.NextIndex, RuntimeRevision: run.RuntimeVersion(), Request: run.Request}
	context, err := driver.service.control.PreviewAgentRecoveryProgress(expected)
	if err != nil {
		return err
	}
	proof, err := worker.PrepareAgentRecoveryProgress(ctx, &agentv1.PrepareAgentRecoveryProgressRequest{SchemaVersion: 1, Context: context, Fence: expected.Fence})
	if err != nil {
		return err
	}
	_, err = driver.service.control.CommitAgentRecoveryProgress(store.CommitAgentRecoveryProgressRequest{AgentRecoveryProgressExpected: expected,
		OperationID: driverOperation("rerun-progress:", run.RunID, run.Recovery.Generation, run.NextIndex, authority.MetadataEpoch, run.Request.SHA256),
		Actor:       agentDriverActor, Receipt: proof})
	return err
}
