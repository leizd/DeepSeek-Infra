package api

import (
	"errors"
	"fmt"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestAgentOutcomeReadFailuresKeepExactPublicStatus(t *testing.T) {
	for _, test := range []struct {
		err     error
		code    codes.Code
		message string
	}{
		{fmt.Errorf("historical receipt: %w", store.ErrCorruptRecord), codes.DataLoss, "AGENT_CONTROL_HISTORY_INVALID"},
		{errors.New("isolated database read failure"), codes.Unavailable, "AGENT_CONTROL_WRITE_FAILED"},
	} {
		failure := status.Convert(nodeDispatchError(test.err))
		if failure.Code() != test.code || failure.Message() != test.message {
			t.Fatalf("a read failure lost its fail-closed public classification: %v", failure)
		}
	}
}
