package worker

import (
	"context"
	"errors"
	"fmt"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

var ErrTargetInspectionResponse = errors.New("BACKUP_TARGET_INSPECTION_RESPONSE_INVALID")

const targetObservationTimeout = 15 * time.Second

func validObservationBinding(binding ControlSignerBinding) bool {
	_, err := store.SignerKeyIDForPublicKey(binding.PublicKey)
	return err == nil && binding.FleetID != "" && binding.Environment != ""
}

func observationContext(binding ControlSignerBinding) store.BackupTargetObservationContext {
	return store.BackupTargetObservationContext{PublicKey: binding.PublicKey, FleetID: binding.FleetID,
		Environment: binding.Environment, Now: time.Now().Unix()}
}

func (client *Client) InspectFilesystemBackupTarget(ctx context.Context, input *actionv1.InspectFilesystemBackupTargetRequest, binding ControlSignerBinding) (*actionv1.FilesystemBackupTargetInspectionReceipt, error) {
	if ctx == nil || store.ValidateBackupTargetInspectionRequest(input) != nil || !validObservationBinding(binding) {
		return nil, ErrTargetInspectionResponse
	}
	if client == nil || !client.tlsSecured || !client.bearerAttached || client.targetInspection == nil {
		return nil, ErrWorkerTLSRequired
	}
	request := proto.Clone(input).(*actionv1.InspectFilesystemBackupTargetRequest)
	bounded, cancel := context.WithTimeout(ctx, targetObservationTimeout)
	defer cancel()
	outgoing, err := client.outgoingContext(bounded, "")
	if err != nil {
		return nil, err
	}
	response, err := client.targetInspection.Inspect(outgoing, request, grpc.MaxCallRecvMsgSize(store.MaxBackupTargetInspectionReceiptBytes))
	if err != nil {
		return nil, fmt.Errorf("target inspection transport: %w", err)
	}
	if err := store.VerifyBackupTargetInspectionReceipt(request, response, observationContext(binding)); err != nil {
		return nil, errors.Join(ErrTargetInspectionResponse, err)
	}
	return proto.Clone(response).(*actionv1.FilesystemBackupTargetInspectionReceipt), nil
}

func (client *Client) ProbeFilesystemBackupTargetLocation(ctx context.Context, input *actionv1.ProbeFilesystemBackupTargetLocationRequest, binding ControlSignerBinding) (*actionv1.FilesystemBackupTargetLocationReceipt, error) {
	if ctx == nil || store.ValidateBackupTargetLocationRequest(input) != nil || !validObservationBinding(binding) {
		return nil, ErrTargetInspectionResponse
	}
	if client == nil || !client.tlsSecured || !client.bearerAttached || client.targetInspection == nil {
		return nil, ErrWorkerTLSRequired
	}
	request := proto.Clone(input).(*actionv1.ProbeFilesystemBackupTargetLocationRequest)
	bounded, cancel := context.WithTimeout(ctx, targetObservationTimeout)
	defer cancel()
	outgoing, err := client.outgoingContext(bounded, "")
	if err != nil {
		return nil, err
	}
	response, err := client.targetInspection.ProbeLocation(outgoing, request, grpc.MaxCallRecvMsgSize(store.MaxBackupTargetLocationReceiptBytes))
	if err != nil {
		return nil, fmt.Errorf("target location transport: %w", err)
	}
	if err := store.VerifyBackupTargetLocationReceipt(request, response, observationContext(binding)); err != nil {
		return nil, errors.Join(ErrTargetInspectionResponse, err)
	}
	return proto.Clone(response).(*actionv1.FilesystemBackupTargetLocationReceipt), nil
}
