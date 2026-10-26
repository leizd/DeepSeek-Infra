package api

import (
	"context"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"os"
)

type mediaMutationService struct {
	mediav1.UnimplementedMediaMutationControlServer
	control *store.Control
	health  ControlPlaneHealth
}

func (s *mediaMutationService) admitted() error {
	if s.control == nil || os.Getenv("DEEPSEEK_RUNTIME_MODE") != "python_disabled" || s.health.Mode != controlv1.RuntimeMode_RUNTIME_MODE_AUTHORITATIVE || s.health.MutationAuthority != config.MutationAuthorityGo {
		return status.Error(codes.PermissionDenied, "MEDIA_AUTHORITY_DENIED")
	}
	return nil
}
func (s *mediaMutationService) GetClaim(_ context.Context, r *mediav1.GetMediaPatchClaimRequest) (*mediav1.MediaPatchClaim, error) {
	if err := s.admitted(); err != nil {
		return nil, err
	}
	if r == nil || r.SchemaVersion != 1 {
		return nil, status.Error(codes.InvalidArgument, "MEDIA_REQUEST_INVALID")
	}
	c, err := s.control.GetMediaPatchClaim(r.OperationId, r.Fence)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, "MEDIA_CLAIM_DENIED")
	}
	return c, nil
}
func (s *mediaMutationService) BeginDispatch(_ context.Context, r *mediav1.BeginMediaPatchDispatchRequest) (*mediav1.MediaPatchDispatchPermit, error) {
	if err := s.admitted(); err != nil {
		return nil, err
	}
	c, err := s.control.BeginMediaPatchDispatch(r)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, "MEDIA_DISPATCH_DENIED")
	}
	return c, nil
}
func (s *mediaMutationService) RecordOutcome(_ context.Context, r *mediav1.RecordMediaPatchOutcomeRequest) (*mediav1.RecordMediaPatchOutcomeResponse, error) {
	if err := s.admitted(); err != nil {
		return nil, err
	}
	if r == nil || r.SchemaVersion != 1 || r.Outcome == nil || r.Fence == nil || !proto.Equal(r.Fence, r.Outcome.OriginalFence) {
		return nil, status.Error(codes.InvalidArgument, "MEDIA_REQUEST_INVALID")
	}
	already, err := s.control.RecordMediaPatchOutcome(r.Outcome)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, "MEDIA_OUTCOME_DENIED")
	}
	return &mediav1.RecordMediaPatchOutcomeResponse{AlreadyRecorded: already}, nil
}
