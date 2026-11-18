package api

import (
	"context"
	"os"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type backupTargetMarkerControl struct {
	actionv1.UnimplementedBackupTargetMarkerControlServer
	control *store.Control
	health  ControlPlaneHealth
}

func (service *backupTargetMarkerControl) BeginDispatch(ctx context.Context, request *actionv1.TargetMarkerDispatchRequest) (*actionv1.TargetMarkerDispatchPermit, error) {
	if service.control == nil || os.Getenv("DEEPSEEK_RUNTIME_MODE") != "python_disabled" ||
		service.health.Mode != controlv1.RuntimeMode_RUNTIME_MODE_AUTHORITATIVE || service.health.MutationAuthority != config.MutationAuthorityGo {
		return nil, status.Error(codes.PermissionDenied, "BACKUP_TARGET_MARKER_AUTHORITY_DENIED")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	permit, err := service.control.BeginTargetMarkerDispatch(request)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, "BACKUP_TARGET_MARKER_CLAIM_DENIED")
	}
	return permit, nil
}
