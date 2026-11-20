package worker

import (
	"context"
	"errors"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"testing"
)

func TestMediaCreationContentRequiresPinnedTLSAndServiceIdentity(t *testing.T) {
	for _, client := range []*Client{nil, {}, {tlsSecured: true}, {bearerAttached: true}, {tlsSecured: true, bearerAttached: true}} {
		if err := client.StageMediaCreate(context.Background(), nil, nil); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatal(err)
		}
		if _, err := client.ExecuteMediaCreate(context.Background(), &mediav1.ExecuteMediaPatchRequest{}); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatal(err)
		}
		if _, err := client.ReadMediaCreateResult(context.Background(), nil); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatal(err)
		}
	}
}
