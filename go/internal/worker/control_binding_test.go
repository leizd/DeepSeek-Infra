package worker

import (
	"context"
	"errors"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type fakeBindingReader func(context.Context, *actionv1.ControlSignerBindingRequest) (*actionv1.ControlSignerBindingResponse, error)

func (fake fakeBindingReader) GetBinding(ctx context.Context, input *actionv1.ControlSignerBindingRequest, _ ...grpc.CallOption) (*actionv1.ControlSignerBindingResponse, error) {
	return fake(ctx, input)
}

func TestReadControlSignerBindingValidatesPublicMetadata(t *testing.T) {
	_, binding, _ := controlSigningFixture()
	keyID, err := store.SignerKeyIDForPublicKey(binding.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	for _, pin := range []string{"", binding.PublicKey} {
		client := &Client{tlsSecured: true, bearerAttached: true, bootstrap: fakeBindingReader(func(ctx context.Context, q *actionv1.ControlSignerBindingRequest) (*actionv1.ControlSignerBindingResponse, error) {
			if ctx == nil || q.SchemaVersion != 1 || q.FleetId != binding.FleetID || q.Environment != binding.Environment || q.FencingToken != 2 || q.ExpectedSignerPublicKey != pin {
				t.Fatal("binding query scope changed")
			}
			return &actionv1.ControlSignerBindingResponse{SchemaVersion: 1, SignerPublicKey: binding.PublicKey, SignerKeyId: keyID, FleetId: binding.FleetID, Environment: binding.Environment, FencingToken: 2}, nil
		})}
		got, err := client.ReadControlSignerBinding(context.Background(), ControlSignerBindingQuery{FleetID: binding.FleetID, Environment: binding.Environment, PublicKey: pin, FencingToken: 2})
		if err != nil || got != binding {
			t.Fatalf("binding metadata: %v", err)
		}
	}
}

func TestReadControlSignerBindingRejectsMalformedResponses(t *testing.T) {
	_, binding, _ := controlSigningFixture()
	keyID, _ := store.SignerKeyIDForPublicKey(binding.PublicKey)
	original := &actionv1.ControlSignerBindingResponse{SchemaVersion: 1, SignerPublicKey: binding.PublicKey, SignerKeyId: keyID, FleetId: binding.FleetID, Environment: binding.Environment, FencingToken: 1}
	cases := map[string]func(*actionv1.ControlSignerBindingResponse){
		"schema": func(r *actionv1.ControlSignerBindingResponse) { r.SchemaVersion = 2 },
		"key":    func(r *actionv1.ControlSignerBindingResponse) { r.SignerPublicKey = "not-a-public-key" },
		"different-pin": func(r *actionv1.ControlSignerBindingResponse) {
			r.SignerPublicKey = strings.Repeat("A", 43)
			r.SignerKeyId, _ = store.SignerKeyIDForPublicKey(r.SignerPublicKey)
		},
		"key-id":      func(r *actionv1.ControlSignerBindingResponse) { r.SignerKeyId = strings.Repeat("0", 64) },
		"fleet":       func(r *actionv1.ControlSignerBindingResponse) { r.FleetId = "foreign-fleet" },
		"environment": func(r *actionv1.ControlSignerBindingResponse) { r.Environment = "foreign" },
		"fence":       func(r *actionv1.ControlSignerBindingResponse) { r.FencingToken = 2 },
		"unknown":     func(r *actionv1.ControlSignerBindingResponse) { r.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			response := proto.Clone(original).(*actionv1.ControlSignerBindingResponse)
			change(response)
			client := &Client{tlsSecured: true, bearerAttached: true, bootstrap: fakeBindingReader(func(context.Context, *actionv1.ControlSignerBindingRequest) (*actionv1.ControlSignerBindingResponse, error) {
				return response, nil
			})}
			_, err := client.ReadControlSignerBinding(context.Background(), ControlSignerBindingQuery{FleetID: binding.FleetID, Environment: binding.Environment, PublicKey: binding.PublicKey, FencingToken: 1})
			if !errors.Is(err, ErrControlSignerResponse) {
				t.Fatalf("malformed binding accepted: %v", err)
			}
		})
	}
	for _, failure := range []bool{false, true} {
		client := &Client{tlsSecured: true, bearerAttached: true, bootstrap: fakeBindingReader(func(context.Context, *actionv1.ControlSignerBindingRequest) (*actionv1.ControlSignerBindingResponse, error) {
			if failure {
				return nil, status.Error(codes.Unavailable, "fixture")
			}
			return nil, nil
		})}
		_, err := client.ReadControlSignerBinding(context.Background(), ControlSignerBindingQuery{FleetID: binding.FleetID, Environment: binding.Environment, FencingToken: 1})
		if failure {
			if status.Code(err) != codes.Unavailable {
				t.Fatal(err)
			}
		} else if !errors.Is(err, ErrControlSignerResponse) {
			t.Fatal(err)
		}
	}
}

func TestReadControlSignerBindingRefusesUntrustedDiscovery(t *testing.T) {
	query := ControlSignerBindingQuery{FleetID: "fleet-a", Environment: "test", FencingToken: 1}
	blocked := fakeBindingReader(func(context.Context, *actionv1.ControlSignerBindingRequest) (*actionv1.ControlSignerBindingResponse, error) {
		t.Fatal("untrusted query reached peer")
		return nil, nil
	})
	for _, client := range []*Client{nil, {}, {tlsSecured: true}, {bearerAttached: true}} {
		if client != nil {
			client.bootstrap = blocked
		}
		if _, err := client.ReadControlSignerBinding(context.Background(), query); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatal(err)
		}
	}
	client := &Client{tlsSecured: true, bearerAttached: true}
	if _, err := client.ReadControlSignerBinding(context.Background(), query); !errors.Is(err, ErrControlSignerResponse) {
		t.Fatal(err)
	}
	client.bootstrap = blocked
	for _, bad := range []string{"", " A", "A", "a/", "a\\", "-a", "a\n", strings.Repeat("a", 129), "é"} {
		for _, fleet := range []bool{false, true} {
			q := query
			if fleet {
				q.FleetID = bad
			} else {
				q.Environment = bad
			}
			if _, err := client.ReadControlSignerBinding(context.Background(), q); !errors.Is(err, ErrControlSignerResponse) {
				t.Fatal(err)
			}
		}
	}
	for _, fence := range []int64{0, -1} {
		q := query
		q.FencingToken = fence
		if _, err := client.ReadControlSignerBinding(context.Background(), q); !errors.Is(err, ErrControlSignerResponse) {
			t.Fatal(err)
		}
	}
	q := query
	q.PublicKey = "invalid"
	if _, err := client.ReadControlSignerBinding(context.Background(), q); !errors.Is(err, ErrControlSignerResponse) {
		t.Fatal(err)
	}
	if _, err := client.ReadControlSignerBinding(nil, query); !errors.Is(err, ErrControlSignerResponse) {
		t.Fatal(err)
	}
	duplicate := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer conflicting-offline-fixture")
	if _, err := client.ReadControlSignerBinding(duplicate, query); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatal(err)
	}
	if !validBindingName("a-._9") {
		t.Fatal("valid profile scope rejected")
	}
}
