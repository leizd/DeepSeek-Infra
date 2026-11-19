package api

import (
	"context"
	"errors"
	"net/http"
	"os"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	targetmodel "github.com/leizd/DeepSeek-Infra/go/internal/targets"
)

func backupTargetRegisterExistingS3(writer http.ResponseWriter, request *http.Request, control *store.Control, input targetmodel.Input, operation, bodySHA string) {
	region, configured := os.LookupEnv("DEEPSEEK_WORKER_S3_REGION")
	if !configured {
		region = "us-east-1"
	}
	placement := targetmodel.S3Placement{
		Endpoint: os.Getenv("DEEPSEEK_WORKER_S3_ENDPOINT"), Region: region,
		Bucket: os.Getenv("DEEPSEEK_WORKER_S3_BUCKET"), Prefix: os.Getenv("DEEPSEEK_WORKER_S3_PREFIX"),
	}
	registration, err := input.ConfiguredS3Registration(placement)
	if errors.Is(err, targetmodel.ErrS3RegistrationUnavailable) {
		policyWriteError(writer, 503, "BACKUP_TARGET_REGISTRATION_UNAVAILABLE", "Registration requires a configured, initialized S3 namespace and a request without a capability probe")
		return
	}
	if err != nil {
		normalizeRefusal(writer, err)
		return
	}
	scope, err := control.PrepareS3BackupTargetRegistration(operation, publicTargetActor, bodySHA, registration)
	if err != nil {
		publicTargetRegistrationFailure(writer, control, operation, bodySHA, err)
		return
	}
	client, err := dialMediaWorker()
	if err != nil {
		policyWriteError(writer, 503, "BACKUP_TARGET_INSPECTION_UNAVAILABLE", "Backup target inspection is unavailable")
		return
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(request.Context(), 15*time.Second)
	defer cancel()
	receipt, err := client.InspectS3BackupTarget(ctx, scope.InspectionRequest(), control.BackupTargetObservationBinding())
	if err != nil {
		publicTargetRegistrationError(writer, err)
		return
	}
	record, err := control.RegisterObservedS3BackupTarget(scope, receipt)
	if err != nil {
		publicTargetRegistrationFailure(writer, control, operation, bodySHA, err)
		return
	}
	writePublicTarget(writer, record)
}
