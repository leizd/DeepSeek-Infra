package api

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	targetmodel "github.com/leizd/DeepSeek-Infra/go/internal/targets"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const publicTargetActor = "operator:public-api"

func publicTargetOperation(request *http.Request) (string, error) {
	if supplied := request.Header.Get("X-DeepSeek-Operation-Id"); supplied != "" {
		if !strings.HasPrefix(supplied, store.PublicTargetOperationPrefix) || len(supplied) > 128 {
			return "", store.ErrTargetRegistrationInvalid
		}
		for _, r := range supplied {
			if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
				return "", store.ErrTargetRegistrationInvalid
			}
		}
		return supplied, nil
	}
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return store.PublicTargetOperationPrefix + hex.EncodeToString(raw), nil
}

func writePublicTarget(writer http.ResponseWriter, record store.Record) {
	public, err := store.PublicBackupTargetRecord(record.Payload)
	if err != nil {
		backupTargetMutationRefusal(writer, err, "registration")
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_, _ = writer.Write(public)
}

func publicTargetRegistrationError(writer http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, store.ErrTargetMarkerPending):
		policyWriteError(writer, 503, "BACKUP_TARGET_MARKER_EFFECT_UNKNOWN", "Backup target initialization is unresolved; retry with the same operation id")
	case errors.Is(err, store.ErrTargetMarkerClaim):
		policyWriteError(writer, 503, "BACKUP_TARGET_INITIALIZATION_UNAVAILABLE", "Backup target initialization could not be authorized")
	case errors.Is(err, worker.ErrTargetInspectionResponse):
		policyWriteError(writer, 503, "BACKUP_TARGET_INSPECTION_UNAVAILABLE", "Backup target inspection is unavailable")
	case errors.Is(err, store.ErrS3BackupTargetLayoutIncomplete):
		policyWriteError(writer, 503, "S3_BACKUP_TARGET_LAYOUT_INCOMPLETE", "The configured S3 namespace has no complete backup target identity and head")
	case errors.Is(err, store.ErrS3BackupTargetHeadConflict):
		policyWriteError(writer, 409, "S3_BACKUP_TARGET_HEAD_CONFLICT", "The observed S3 head conflicts with the retained target history")
	case errors.Is(err, store.ErrTargetOverlap):
		policyWriteError(writer, 400, "invalid_payload", "Unsafe backup target: target overlaps a registered target")
	case errors.Is(err, store.ErrTargetCloneDetected):
		policyWriteError(writer, 409, "invalid_request", "target-clone-detected: target id is registered at another location that still exists; adopt one branch or register it as new")
	case errors.Is(err, store.ErrRevisionConflict):
		policyWriteError(writer, 409, "revision_conflict", "Backup target registry changed during registration; retry")
	case errors.Is(err, store.ErrMutationRequestReplayConflict):
		policyWriteError(writer, 409, "mutation_request_replay_conflict", "Operation id was already used for another request")
	case errors.Is(err, store.ErrTargetRegistrationInvalid), errors.Is(err, store.ErrTargetLocationRequired):
		policyWriteError(writer, 400, "invalid_payload", "Backup target registration evidence is invalid")
	default:
		switch status.Code(err) {
		case codes.InvalidArgument:
			policyWriteError(writer, 400, "invalid_payload", status.Convert(err).Message())
		case codes.Canceled, codes.DeadlineExceeded, codes.Unavailable, codes.ResourceExhausted:
			policyWriteError(writer, 503, "BACKUP_TARGET_INSPECTION_UNAVAILABLE", "Backup target inspection is unavailable")
		case codes.Unauthenticated, codes.PermissionDenied, codes.FailedPrecondition:
			policyWriteError(writer, 503, "BACKUP_TARGET_INSPECTION_DENIED", "Backup target inspection could not be authorized")
		default:
			backupTargetMutationRefusal(writer, err, "registration")
		}
	}
}

func publicTargetRegistrationFailure(writer http.ResponseWriter, control *store.Control, operation, bodySHA string, err error) {
	if errors.Is(err, store.ErrMutationRequestReplayConflict) || errors.Is(err, store.ErrRevisionConflict) || errors.Is(err, store.ErrTargetMarkerPending) {
		// The original body may have committed while these observations were in
		// flight, including before competing paths were read from the registry.
		record, _, found, recoveryErr := control.ReplayPublicBackupTargetRegistration(operation, publicTargetActor, bodySHA)
		if recoveryErr == nil && found {
			writePublicTarget(writer, record)
			return
		}
		if recoveryErr != nil {
			err = recoveryErr
		}
	}
	publicTargetRegistrationError(writer, err)
}

func backupTargetRegister(writer http.ResponseWriter, request *http.Request, control *store.Control, view PublicView) {
	if !authorizationAndHost(writer, request) {
		return
	}
	if control == nil || view.Mode != config.ModeAuthoritative || view.MutationAuthority != config.MutationAuthorityGo {
		policyWriteError(writer, 503, "GO_CONTROL_NOT_AUTHORITATIVE", "Backup targets have not completed native ownership cutover")
		return
	}
	raw, err := io.ReadAll(io.LimitReader(request.Body, maximumPolicyBodyBytes+1))
	if err != nil {
		policyWriteError(writer, 400, "invalid_payload", "Request body is unreadable")
		return
	}
	if len(raw) > maximumPolicyBodyBytes {
		policyWriteError(writer, 413, "request_too_large", "Backup target payload is too large")
		return
	}
	operation, err := publicTargetOperation(request)
	if err != nil {
		publicTargetRegistrationError(writer, err)
		return
	}
	writer.Header().Set("X-DeepSeek-Operation-Id", operation)
	digest := sha256.Sum256(raw)
	bodySHA := hex.EncodeToString(digest[:])
	if record, _, found, err := control.ReplayPublicBackupTargetRegistration(operation, publicTargetActor, bodySHA); err != nil {
		publicTargetRegistrationError(writer, err)
		return
	} else if found {
		writePublicTarget(writer, record)
		return
	}
	input, err := targetmodel.ReadInput(raw)
	if err != nil {
		normalizeRefusal(writer, err)
		return
	}
	if input.Kind() == "webdav" {
		policyWriteError(writer, 501, "invalid_request", "WebDAV targets are reserved but not GA in 4.4.6")
		return
	}
	if input.Kind() == "s3" || input.Kind() == "s3-compatible" {
		backupTargetRegisterExistingS3(writer, request, control, input, operation, bodySHA)
		return
	}
	path := input.Path()
	if !filepath.IsAbs(path) {
		policyWriteError(writer, 400, "invalid_payload", "Backup target path must be absolute")
		return
	}
	native := os.Getenv("DEEPSEEK_RUNTIME_MODE") == "python_disabled"
	var original store.TargetMarkerOperation
	var pending bool
	if native {
		original, pending, err = control.PendingTargetMarker(operation, publicTargetActor, bodySHA)
		if err != nil {
			publicTargetRegistrationFailure(writer, control, operation, bodySHA, err)
			return
		}
	}
	scope, err := control.PrepareBackupTargetRegistration(operation, operation, publicTargetActor, path)
	if err != nil {
		publicTargetRegistrationError(writer, err)
		return
	}
	client, err := dialMediaWorker()
	if err != nil {
		if pending {
			publicTargetRegistrationFailure(writer, control, operation, bodySHA, store.ErrTargetMarkerPending)
		} else {
			policyWriteError(writer, 503, "BACKUP_TARGET_INSPECTION_UNAVAILABLE", "Backup target inspection is unavailable")
		}
		return
	}
	defer client.Close()
	pinned := control.BackupTargetObservationBinding()
	binding := worker.ControlSignerBinding{PublicKey: pinned.PublicKey, FleetID: pinned.FleetID, Environment: pinned.Environment}
	ctx, cancel := context.WithTimeout(request.Context(), 45*time.Second)
	defer cancel()
	if pending {
		completeTargetMarkerRegistration(ctx, writer, control, client, scope, binding, input, operation, bodySHA, original)
		return
	}
	inspection, err := client.InspectFilesystemBackupTarget(ctx, scope.InspectionRequest(), binding)
	if err != nil {
		publicTargetRegistrationError(writer, err)
		return
	}
	if !inspection.Observation.MarkerPresent {
		if !native || len(inspection.Observation.DirectoryIdentity) != 32 {
			policyWriteError(writer, 503, "BACKUP_TARGET_INITIALIZATION_UNAVAILABLE", "Backup target initialization is unavailable")
			return
		}
		if err := input.ValidateFilesystemMetadata(); err != nil {
			normalizeRefusal(writer, err)
			return
		}
		proofs, err := inspectCompetingTargetPaths(ctx, control, client, scope, binding, "")
		if err != nil {
			publicTargetRegistrationFailure(writer, control, operation, bodySHA, err)
			return
		}
		original, err := control.AdmitTargetMarker(scope, inspection, proofs, bodySHA)
		if err != nil {
			publicTargetRegistrationFailure(writer, control, operation, bodySHA, err)
			return
		}
		completeTargetMarkerRegistration(ctx, writer, control, client, scope, binding, input, operation, bodySHA, original)
		return
	}
	id, _, _, err := targetmodel.MarkerIdentity(inspection.Observation.MarkerJson)
	if err != nil {
		normalizeRefusal(writer, err)
		return
	}
	proofs, err := inspectCompetingTargetPaths(ctx, control, client, scope, binding, id)
	if err != nil {
		publicTargetRegistrationFailure(writer, control, operation, bodySHA, err)
		return
	}
	var oldLocation *actionv1.FilesystemBackupTargetLocationReceipt
	if old, found, err := control.Get("target", id); err != nil {
		publicTargetRegistrationError(writer, err)
		return
	} else if found && old.State != store.TombstoneState {
		var body map[string]any
		decoder := json.NewDecoder(strings.NewReader(string(old.Payload)))
		decoder.UseNumber()
		if decoder.Decode(&body) != nil {
			publicTargetRegistrationError(writer, store.ErrCorruptRecord)
			return
		}
		previous, _ := body["path"].(string)
		if previous == "" {
			publicTargetRegistrationError(writer, store.ErrTargetCloneDetected)
			return
		}
		same := store.SameBackupTargetLocation(previous, inspection.Observation.CanonicalPath)
		if !same {
			oldLocation, err = client.ProbeFilesystemBackupTargetLocation(ctx, scope.LocationRequest(previous), binding)
			if err != nil {
				publicTargetRegistrationError(writer, err)
				return
			}
		}
	}
	document, err := input.FilesystemDocument(inspection.Observation.MarkerJson, inspection.Observation.CanonicalPath, inspection.Observation.ObservedAtUnix)
	if err != nil {
		normalizeRefusal(writer, err)
		return
	}
	record, _, err := control.RegisterPublicInspectedBackupTarget(scope, inspection, oldLocation, proofs, bodySHA, document)
	if err != nil {
		publicTargetRegistrationFailure(writer, control, operation, bodySHA, err)
		return
	}
	writePublicTarget(writer, record)
}

func inspectCompetingTargetPaths(ctx context.Context, control *store.Control, client *worker.Client,
	scope store.BackupTargetRegistrationScope, binding worker.ControlSignerBinding, exclude string) ([]store.BackupTargetPathProof, error) {
	paths, err := control.BackupTargetRegistrationPaths(scope, exclude)
	if err != nil {
		return nil, err
	}
	proofs := make([]store.BackupTargetPathProof, 0, len(paths))
	for _, candidate := range paths {
		read := scope.LocationRequest(candidate.Path)
		read.ResolvePath = true
		receipt, err := client.ProbeFilesystemBackupTargetLocation(ctx, read, binding)
		if err != nil {
			return nil, err
		}
		proofs = append(proofs, store.BackupTargetPathProof{TargetID: candidate.TargetID, Receipt: receipt})
	}
	return proofs, nil
}
