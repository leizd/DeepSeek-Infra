package worker

import (
	"context"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"testing"
)

func TestMediaCallsRequireTheProductionSecuredWorkerChannel(t *testing.T) {
	for _, client := range []*Client{nil, New(nil), {tlsSecured: true}, {bearerAttached: true}} {
		if _, err := client.ExecuteMediaPatch(context.Background(), &mediav1.ExecuteMediaPatchRequest{}); err != ErrWorkerTLSRequired {
			t.Fatalf("execution accepted unpinned channel: %v", err)
		}
		if _, err := client.QueryMediaPatch(context.Background(), &mediav1.QueryMediaPatchRequest{}); err != ErrWorkerTLSRequired {
			t.Fatalf("query accepted unpinned channel: %v", err)
		}
		if _, err := client.ExecuteMediaDelete(context.Background(), &mediav1.ExecuteMediaDeleteRequest{}); err != ErrWorkerTLSRequired {
			t.Fatalf("delete accepted unpinned channel: %v", err)
		}
	}
}
