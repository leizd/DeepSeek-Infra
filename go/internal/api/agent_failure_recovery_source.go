package api

import (
	"bytes"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

// A FAILED label is not permission to retry. Only the exact committed recovery
// scope may retire a prior action, after its original typed settlement and Rust
// outcome have independently passed the store's retained-history verification.
func (driver *agentRunDriver) verifiedPriorFailure(run agent.RunMetadata, record store.Record) error {
	scope := run.Recovery
	if record.State != "FAILED" || scope == nil || scope.Metadata == nil {
		return errAgentDriverUnknown
	}
	binding, err := store.AgentRunExecutionBindingFromRecord(record)
	if err != nil || binding.RunID != run.RunID || binding.MetadataIndex >= scope.StartIndex {
		return errAgentDriverUnknown
	}
	node := "leader"
	if binding.Phase == "synthesis" {
		node = "synthesizer"
	} else if binding.Phase != "plan" {
		return errAgentDriverUnknown
	}
	current, found, err := driver.service.control.AgentRunMetadata(run.RunID)
	if err != nil {
		return err
	}
	if !found || current.Recovery == nil || current.Status != run.Status || current.NextIndex != run.NextIndex ||
		current.RuntimeVersion() != run.RuntimeVersion() || current.Request != run.Request ||
		current.Recovery.Generation != scope.Generation || current.Recovery.StartIndex != scope.StartIndex ||
		current.Recovery.Context != scope.Context || current.Recovery.ContextSHA256 != scope.ContextSHA256 ||
		!proto.Equal(current.Recovery.Metadata, scope.Metadata) {
		return errAgentDriverUnknown
	}
	retained, dispatch, receipt, found, err := driver.service.control.AgentNodeOutcomeSnapshot(record.ID, node)
	if err != nil {
		return err
	}
	if !found || retained.State != "FAILED" || retained.ID != record.ID || retained.Revision != record.Revision ||
		retained.ExecutionEpoch != record.ExecutionEpoch || !bytes.Equal(retained.Payload, record.Payload) ||
		receipt == nil || !agentproof.ValidTerminalFailure(receipt.Evidence) {
		return errAgentDriverCustody
	}
	matching := 0
	for _, source := range scope.Metadata.PriorEffects {
		if source != nil && proto.Equal(source.Dispatch, nodeDispatchProtocol(dispatch)) && proto.Equal(source.Outcome, receipt) {
			matching++
		}
	}
	if matching == 1 {
		return nil
	}
	// Older actions may already be covered by the preceding committed scope.
	// Its header is part of the exact stored current context checked above;
	// the original terminal receipt is still verified afresh in all cases.
	if matching == 0 && scope.Metadata.PreviousScope != nil && binding.MetadataIndex < scope.Metadata.PreviousScope.StartIndex {
		return nil
	}
	return errAgentDriverCustody
}
