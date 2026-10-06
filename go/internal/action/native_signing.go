package action

import (
	"context"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

func (c *Coordinator) authorizeNativeStorage(ctx context.Context, input *actionv1.StorageMutationRequest, revision, fencingToken int64) (*actionv1.StorageMutationRequest, error) {
	signer, ok := c.worker.(interface {
		AuthorizeStorage(context.Context, *actionv1.StorageMutationRequest, int64, int64) (*actionv1.StorageMutationRequest, error)
	})
	if !ok {
		return nil, internalprotocol.ErrAuthenticationMissing
	}
	unsigned := proto.Clone(input).(*actionv1.StorageMutationRequest)
	authorized, err := signer.AuthorizeStorage(ctx, proto.Clone(unsigned).(*actionv1.StorageMutationRequest), revision, fencingToken)
	if err != nil {
		return nil, err
	}
	if authorized == nil || len(authorized.CanonicalAuthorization) == 0 {
		return nil, ErrStorageMutationUncertain
	}
	returned := proto.Clone(authorized).(*actionv1.StorageMutationRequest)
	returned.CanonicalAuthorization = nil
	if !proto.Equal(returned, unsigned) {
		return nil, ErrStorageMutationUncertain
	}
	return authorized, nil
}
