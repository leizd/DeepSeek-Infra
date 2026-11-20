package worker

import (
	"context"
	"math"
	"strings"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

func (client *Client) nativeOriginContext(ctx context.Context, in *actionv1.ReadNativeOriginRequest) (context.Context, error) {
	if client == nil || !client.tlsSecured || !client.bearerAttached {
		return nil, ErrWorkerTLSRequired
	}
	if ctx == nil || in == nil || client.origin == nil || in.SchemaVersion != 1 || len(in.ProtoReflect().GetUnknown()) != 0 ||
		len(in.ProfileId) != 32 || strings.Trim(in.ProfileId, "0123456789abcdef") != "" || in.Fence == nil || len(in.Fence.ProtoReflect().GetUnknown()) != 0 ||
		in.Fence.ActionId != "native-origin:"+in.ProfileId || in.Fence.ExecutionEpoch != 1 || in.WriterFencingToken == 0 || in.WriterFencingToken > math.MaxInt64 {
		return nil, store.ErrNativeOrigin
	}
	if _, err := store.SignerKeyIDForPublicKey(in.ExpectedSignerPublicKey); err != nil {
		return nil, store.ErrNativeOrigin
	}
	return client.outgoingContext(ctx, "")
}
func boundedNativeOrigin(state *actionv1.NativeOriginState, err error) (*actionv1.NativeOriginState, error) {
	if err != nil {
		return nil, err
	}
	if state == nil || state.SchemaVersion != 1 || len(state.ProtoReflect().GetUnknown()) != 0 || proto.Size(state) > 32768 {
		return nil, store.ErrNativeOrigin
	}
	return proto.Clone(state).(*actionv1.NativeOriginState), nil
}
func (client *Client) ReadNativeOrigin(ctx context.Context, input *actionv1.ReadNativeOriginRequest) (*actionv1.NativeOriginState, error) {
	outgoing, err := client.nativeOriginContext(ctx, input)
	if err != nil {
		return nil, err
	}
	return boundedNativeOrigin(client.origin.Read(outgoing, proto.Clone(input).(*actionv1.ReadNativeOriginRequest)))
}
func (client *Client) PublishNativeOrigin(ctx context.Context, input *actionv1.PublishNativeOriginRequest) (*actionv1.NativeOriginState, error) {
	if input == nil || len(input.ProtoReflect().GetUnknown()) != 0 || len(input.OriginSha256) != 64 || strings.Trim(input.OriginSha256, "0123456789abcdef") != "" {
		return nil, store.ErrNativeOrigin
	}
	query := &actionv1.ReadNativeOriginRequest{SchemaVersion: input.SchemaVersion, Fence: input.Fence, ProfileId: input.ProfileId, ExpectedSignerPublicKey: input.ExpectedSignerPublicKey, WriterFencingToken: input.WriterFencingToken}
	outgoing, err := client.nativeOriginContext(ctx, query)
	if err != nil {
		return nil, err
	}
	return boundedNativeOrigin(client.origin.Publish(outgoing, proto.Clone(input).(*actionv1.PublishNativeOriginRequest)))
}
