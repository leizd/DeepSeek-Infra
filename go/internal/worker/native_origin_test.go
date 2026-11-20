package worker

import (
	"context"
	"errors"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

type nativeOriginRPCFixture struct {
	state            *actionv1.NativeOriginState
	reads, publishes int
}

func (f *nativeOriginRPCFixture) Read(_ context.Context, _ *actionv1.ReadNativeOriginRequest, _ ...grpc.CallOption) (*actionv1.NativeOriginState, error) {
	f.reads++
	return f.state, nil
}
func (f *nativeOriginRPCFixture) Publish(_ context.Context, _ *actionv1.PublishNativeOriginRequest, _ ...grpc.CallOption) (*actionv1.NativeOriginState, error) {
	f.publishes++
	return f.state, nil
}
func nativeOriginQuery() *actionv1.ReadNativeOriginRequest {
	_, binding, _ := controlSigningFixture()
	profile := "0123456789abcdef0123456789abcdef"
	return &actionv1.ReadNativeOriginRequest{SchemaVersion: 1, Fence: &commonv1.ActionFence{ActionId: "native-origin:" + profile, ExecutionEpoch: 1}, ProfileId: profile, ExpectedSignerPublicKey: binding.PublicKey, WriterFencingToken: 2}
}
func TestNativeOriginClientRequiresAuthenticatedTLSAndOriginalBirthFence(t *testing.T) {
	fake := &nativeOriginRPCFixture{state: &actionv1.NativeOriginState{SchemaVersion: 1}}
	for _, client := range []*Client{nil, {}, {origin: fake, tlsSecured: true}, {origin: fake, bearerAttached: true}} {
		if _, err := client.ReadNativeOrigin(context.Background(), nativeOriginQuery()); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatalf("untrusted origin transport: %v", err)
		}
	}
	trusted := &Client{origin: fake, tlsSecured: true, bearerAttached: true}
	for _, kind := range []string{"profile", "epoch", "writer", "key", "unknown"} {
		t.Run(kind, func(t *testing.T) {
			in := nativeOriginQuery()
			switch kind {
			case "profile":
				in.ProfileId = "foreign"
			case "epoch":
				in.Fence.ExecutionEpoch = 2
			case "writer":
				in.WriterFencingToken = 0
			case "key":
				in.ExpectedSignerPublicKey = ""
			case "unknown":
				in.ProtoReflect().SetUnknown([]byte{0x78, 1})
			}
			if _, err := trusted.ReadNativeOrigin(context.Background(), in); !errors.Is(err, store.ErrNativeOrigin) {
				t.Fatalf("invalid query: %v", err)
			}
		})
	}
	if fake.reads != 0 || fake.publishes != 0 {
		t.Fatal("invalid query reached custody")
	}
	state, err := trusted.ReadNativeOrigin(context.Background(), nativeOriginQuery())
	if err != nil || state.SchemaVersion != 1 || fake.reads != 1 {
		t.Fatalf("valid read: %v", err)
	}
	state.SchemaVersion = 9
	if fake.state.SchemaVersion != 1 {
		t.Fatal("response aliases transport-owned state")
	}
}
func TestNativeOriginClientBoundsResponsesAndRefusesUnscopedPublication(t *testing.T) {
	fake := &nativeOriginRPCFixture{state: &actionv1.NativeOriginState{SchemaVersion: 1}}
	client := &Client{origin: fake, tlsSecured: true, bearerAttached: true}
	q := nativeOriginQuery()
	for _, response := range []*actionv1.NativeOriginState{nil, {SchemaVersion: 2}, {SchemaVersion: 1, Origin: &actionv1.NativeOriginReceipt{Signature: make([]byte, 32768)}}} {
		fake.state = response
		if _, err := client.ReadNativeOrigin(context.Background(), q); !errors.Is(err, store.ErrNativeOrigin) {
			t.Fatalf("invalid response accepted: %v", err)
		}
	}
	fake.state = &actionv1.NativeOriginState{SchemaVersion: 1}
	fake.state.ProtoReflect().SetUnknown([]byte{0x78, 1})
	if _, err := client.ReadNativeOrigin(context.Background(), q); !errors.Is(err, store.ErrNativeOrigin) {
		t.Fatal("unknown response field accepted")
	}
	fake.state = &actionv1.NativeOriginState{SchemaVersion: 1}
	in := &actionv1.PublishNativeOriginRequest{SchemaVersion: q.SchemaVersion, Fence: proto.Clone(q.Fence).(*commonv1.ActionFence), ProfileId: q.ProfileId, ExpectedSignerPublicKey: q.ExpectedSignerPublicKey, WriterFencingToken: q.WriterFencingToken, OriginSha256: strings.Repeat("a", 64)}
	if _, err := client.PublishNativeOrigin(context.Background(), in); err != nil || fake.publishes != 1 {
		t.Fatalf("fixed publication: %v", err)
	}
	in.OriginSha256 = "arbitrary document"
	if _, err := client.PublishNativeOrigin(context.Background(), in); !errors.Is(err, store.ErrNativeOrigin) || fake.publishes != 1 {
		t.Fatal("unscoped publication reached custody")
	}
}
