package store

import (
	"errors"
	"path/filepath"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

func TestTargetLocationResolutionRequiresSignedRequestedCanonicalPath(t *testing.T) {
	inspection, _, context := targetObservationFixture(t)
	request, receipt := targetLocationFixture(t, inspection)
	request.ResolvePath = true
	if err := VerifyBackupTargetLocationReceipt(request, receipt, context); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("existence-only response downgraded canonical resolution", err)
	}
	receipt.Observation.CanonicalPath = filepath.Join(t.TempDir(), "resolved", "missing")
	receipt.Observation.State = actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_UNSPECIFIED
	signTargetLocationFixture(t, receipt, targetLocationSignatureDomain)
	if err := VerifyBackupTargetLocationReceipt(request, receipt, context); err != nil {
		t.Fatal("bound signed non-strict resolution rejected", err)
	}
	for name, value := range map[string]string{"empty": "", "relative": "relative", "nul": receipt.Observation.CanonicalPath + "\x00", "oversize": receipt.Observation.CanonicalPath + strings.Repeat("x", 32769)} {
		t.Run(name, func(t *testing.T) {
			bad := proto.Clone(receipt).(*actionv1.FilesystemBackupTargetLocationReceipt)
			bad.Observation.CanonicalPath = value
			signTargetLocationFixture(t, bad, targetLocationSignatureDomain)
			if err := VerifyBackupTargetLocationReceipt(request, bad, context); !errors.Is(err, ErrTargetRegistrationInvalid) {
				t.Fatal("malformed signed canonical result accepted", err)
			}
		})
	}
	tampered := proto.Clone(receipt).(*actionv1.FilesystemBackupTargetLocationReceipt)
	tampered.Observation.CanonicalPath += "-other"
	if err := VerifyBackupTargetLocationReceipt(request, tampered, context); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("unsigned resolution substitution accepted", err)
	}
	request.ResolvePath = false
	if err := VerifyBackupTargetLocationReceipt(request, receipt, context); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("unrequested canonical result accepted as the old clone witness", err)
	}
}
