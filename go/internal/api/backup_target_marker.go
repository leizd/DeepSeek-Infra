package api

import (
	"context"
	"net/http"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	targetmodel "github.com/leizd/DeepSeek-Infra/go/internal/targets"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
)

// A fresh signed read must match the original Rust effect, even when the current
// writer is the successor of the writer that granted the one-use permit.
func completeTargetMarkerRegistration(ctx context.Context, writer http.ResponseWriter, control *store.Control, client *worker.Client,
	scope store.BackupTargetRegistrationScope, binding worker.ControlSignerBinding, input targetmodel.Input, op, bodySHA string,
	original store.TargetMarkerOperation) {
	unknown := func() {
		publicTargetRegistrationFailure(writer, control, op, bodySHA, store.ErrTargetMarkerPending)
	}
	request := scope.InspectionRequest()
	receipt, err := client.ResolveTargetMarker(ctx, original, request.WriterFencingToken)
	if err != nil {
		unknown()
		return
	}
	inspection, err := client.InspectFilesystemBackupTarget(ctx, request, binding)
	if err != nil || !inspection.Observation.MarkerPresent {
		unknown()
		return
	}
	proofs, err := inspectCompetingTargetPaths(ctx, control, client, scope, binding, original.Claim.TargetId)
	if err != nil {
		unknown()
		return
	}
	observation := inspection.Observation
	document, err := input.FilesystemDocument(observation.MarkerJson, observation.CanonicalPath, observation.ObservedAtUnix)
	if err != nil {
		unknown()
		return
	}
	record, _, err := control.RegisterPublicInitializedBackupTarget(scope, inspection, proofs, bodySHA, document, receipt)
	if err != nil {
		unknown()
		return
	}
	writePublicTarget(writer, record)
}
