package worker

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"net"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Go peer signatures test client transport/verification only. The real Rust
// worker and custody integration must be qualified independently in the cloud.
type observationPeer struct {
	actionv1.UnimplementedBackupTargetInspectionServer
	private         ed25519.PrivateKey
	calls           atomic.Int64
	binding         ControlSignerBinding
	absent          bool
	wait            bool
	fullContainment bool
	state           actionv1.BackupTargetLocationState
	alterInspection func(*actionv1.FilesystemBackupTargetInspectionReceipt)
	alterLocation   func(*actionv1.FilesystemBackupTargetLocationReceipt)
}

func (peer *observationPeer) authorize(ctx context.Context) error {
	peer.calls.Add(1)
	md, _ := metadata.FromIncomingContext(ctx)
	if values := md.Get("authorization"); len(values) != 1 || values[0] != "Bearer "+tlsTestSecret {
		return status.Error(codes.Unauthenticated, "expected isolated controller credential")
	}
	if peer.wait {
		<-ctx.Done()
		return status.FromContextError(ctx.Err()).Err()
	}
	return nil
}

func (peer *observationPeer) Inspect(ctx context.Context, request *actionv1.InspectFilesystemBackupTargetRequest) (*actionv1.FilesystemBackupTargetInspectionReceipt, error) {
	if err := peer.authorize(ctx); err != nil {
		return nil, err
	}
	marker := []byte(`{"targetId":"target_original","targetNonce":"retained"}`)
	markerDigest := sha256.Sum256(marker)
	observation := &actionv1.FilesystemBackupTargetObservation{SchemaVersion: 1, Fence: request.Fence, OperationId: request.OperationId,
		WriterFencingToken: request.WriterFencingToken, RequestedPath: request.Path, CanonicalPath: filepath.Clean(request.Path),
		MarkerPresent: true, MarkerJson: marker, MarkerSha256: markerDigest[:], ObservedAtUnix: time.Now().Unix(),
		FleetId: peer.binding.FleetID, Environment: peer.binding.Environment, FullContainmentChecked: request.RequireFullContainment && peer.fullContainment}
	if peer.absent {
		observation.MarkerPresent = false
		observation.MarkerJson = nil
		observation.MarkerSha256 = nil
	}
	wire, _ := proto.MarshalOptions{Deterministic: true}.Marshal(observation)
	receipt := &actionv1.FilesystemBackupTargetInspectionReceipt{SchemaVersion: 1, Observation: observation, SignerPublicKey: peer.binding.PublicKey,
		Signature: ed25519.Sign(peer.private, append([]byte("deepseek-infra:backup-target-inspection-v1\x00"), wire...))}
	if peer.alterInspection != nil {
		peer.alterInspection(receipt)
	}
	return receipt, nil
}

func (peer *observationPeer) ProbeLocation(ctx context.Context, request *actionv1.ProbeFilesystemBackupTargetLocationRequest) (*actionv1.FilesystemBackupTargetLocationReceipt, error) {
	if err := peer.authorize(ctx); err != nil {
		return nil, err
	}
	observation := &actionv1.FilesystemBackupTargetLocationObservation{SchemaVersion: 1, Fence: request.Fence, OperationId: request.OperationId,
		WriterFencingToken: request.WriterFencingToken, RequestedPath: request.Path, State: peer.state, ObservedAtUnix: time.Now().Unix(),
		FleetId: peer.binding.FleetID, Environment: peer.binding.Environment}
	if request.ResolvePath {
		observation.CanonicalPath = filepath.Clean(request.Path)
		observation.State = actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_UNSPECIFIED
	}
	wire, _ := proto.MarshalOptions{Deterministic: true}.Marshal(observation)
	receipt := &actionv1.FilesystemBackupTargetLocationReceipt{SchemaVersion: 1, Observation: observation, SignerPublicKey: peer.binding.PublicKey,
		Signature: ed25519.Sign(peer.private, append([]byte("deepseek-infra:backup-target-location-v1\x00"), wire...))}
	if peer.alterLocation != nil {
		peer.alterLocation(receipt)
	}
	return receipt, nil
}

func targetObservationTLSClient(t *testing.T, peer *observationPeer) *Client {
	t.Helper()
	material := generateTLSMaterial(t, t.TempDir(), "deepseek-worker.test")
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	creds, err := credentials.NewServerTLSFromFile(material.certFile, material.keyFile)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(creds))
	actionv1.RegisterBackupTargetInspectionServer(server, peer)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	client, err := DialTLS(TLSDialConfig{Target: listener.Addr().String(), TrustRootFile: material.caFile,
		ServerName: "deepseek-worker.test", BearerToken: tlsTestSecret, ExpiresAt: time.Now().Add(30 * time.Minute)})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	return client
}

func newObservationPeer(t *testing.T) *observationPeer {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return &observationPeer{private: private, binding: ControlSignerBinding{PublicKey: base64.RawURLEncoding.EncodeToString(public), FleetID: "fleet-a", Environment: "test"}, state: actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS}
}

func targetObservationRequest(t *testing.T) *actionv1.InspectFilesystemBackupTargetRequest {
	t.Helper()
	return &actionv1.InspectFilesystemBackupTargetRequest{SchemaVersion: 1, OperationId: "target-inspect-client",
		Fence: &commonv1.ActionFence{ActionId: "target-inspect-client", ExecutionEpoch: 3}, Path: filepath.Join(t.TempDir(), "target"), WriterFencingToken: 7}
}

func targetLocationRequest(request *actionv1.InspectFilesystemBackupTargetRequest) *actionv1.ProbeFilesystemBackupTargetLocationRequest {
	return &actionv1.ProbeFilesystemBackupTargetLocationRequest{SchemaVersion: request.SchemaVersion, OperationId: request.OperationId,
		Fence: proto.Clone(request.Fence).(*commonv1.ActionFence), Path: request.Path, WriterFencingToken: request.WriterFencingToken}
}

func TestTargetInspectionClientBindsCanonicalResolutionAndRejectsDowngrade(t *testing.T) {
	peer := newObservationPeer(t)
	client := targetObservationTLSClient(t, peer)
	request := targetLocationRequest(targetObservationRequest(t))
	request.ResolvePath = true
	receipt, err := client.ProbeFilesystemBackupTargetLocation(context.Background(), request, peer.binding)
	if err != nil || receipt == nil || receipt.Observation.CanonicalPath != filepath.Clean(request.Path) {
		t.Fatal("pinned canonical resolution failed", err)
	}
	downgrade := newObservationPeer(t)
	downgrade.alterLocation = func(receipt *actionv1.FilesystemBackupTargetLocationReceipt) {
		receipt.Observation.CanonicalPath = ""
		wire, marshalErr := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Observation)
		if marshalErr != nil {
			panic(marshalErr)
		}
		receipt.Signature = ed25519.Sign(downgrade.private, append([]byte("deepseek-infra:backup-target-location-v1\x00"), wire...))
	}
	downgradeClient := targetObservationTLSClient(t, downgrade)
	if _, err := downgradeClient.ProbeFilesystemBackupTargetLocation(context.Background(), request, downgrade.binding); !errors.Is(err, ErrTargetInspectionResponse) {
		t.Fatal("signed existence-only downgrade accepted", err)
	}
}

func TestTargetInspectionClientReadsBothStatesWithTLSAndPinnedPublicMetadata(t *testing.T) {
	for _, absent := range []bool{false, true} {
		peer := newObservationPeer(t)
		peer.absent = absent
		if absent {
			peer.state = actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING
		}
		client := targetObservationTLSClient(t, peer)
		request := targetObservationRequest(t)
		receipt, err := client.InspectFilesystemBackupTarget(context.Background(), request, peer.binding)
		if err != nil || receipt == nil || receipt.Observation.MarkerPresent == absent {
			t.Fatalf("pinned TLS observation failed: %+v %v", receipt, err)
		}
		location, err := client.ProbeFilesystemBackupTargetLocation(context.Background(), targetLocationRequest(request), peer.binding)
		if err != nil || location == nil || location.Observation.State != peer.state {
			t.Fatalf("pinned TLS existence observation failed: %+v %v", location, err)
		}
	}
}

func TestTargetInspectionClientRejectsUnboundResponsesAndTransportFailures(t *testing.T) {
	for _, change := range []func(*actionv1.FilesystemBackupTargetInspectionReceipt){
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Signature[0] ^= 1 },
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.RequestedPath += "-other" },
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Observation.Fence.ExecutionEpoch++ },
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.SignerPublicKey = "unpinned" },
	} {
		peer := newObservationPeer(t)
		peer.alterInspection = change
		client := targetObservationTLSClient(t, peer)
		if _, err := client.InspectFilesystemBackupTarget(context.Background(), targetObservationRequest(t), peer.binding); !errors.Is(err, ErrTargetInspectionResponse) {
			t.Fatalf("unbound marker observation accepted: %v", err)
		}
	}
	peer := newObservationPeer(t)
	peer.alterLocation = func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.RequestedPath += "-other" }
	client := targetObservationTLSClient(t, peer)
	request := targetObservationRequest(t)
	if _, err := client.ProbeFilesystemBackupTargetLocation(context.Background(), targetLocationRequest(request), peer.binding); !errors.Is(err, ErrTargetInspectionResponse) {
		t.Fatalf("unbound location observation accepted: %v", err)
	}
	peer = newObservationPeer(t)
	peer.wait = true
	client = targetObservationTLSClient(t, peer)
	for _, location := range []bool{false, true} {
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		var err error
		if location {
			_, err = client.ProbeFilesystemBackupTargetLocation(ctx, targetLocationRequest(request), peer.binding)
		} else {
			_, err = client.InspectFilesystemBackupTarget(ctx, request, peer.binding)
		}
		cancel()
		if status.Code(err) != codes.DeadlineExceeded {
			t.Fatalf("observation exceeded its caller deadline: %v", err)
		}
	}
}

func TestTargetInspectionClientNeverFallsBackToPlaintextOrDuplicateCredentials(t *testing.T) {
	request := targetObservationRequest(t)
	peer := newObservationPeer(t)
	for _, client := range []*Client{nil, New(nil), {tlsSecured: true}, {tlsSecured: true, bearerAttached: true}} {
		if _, err := client.InspectFilesystemBackupTarget(context.Background(), request, peer.binding); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatalf("incomplete TLS client accepted a read: %v", err)
		}
		if _, err := client.ProbeFilesystemBackupTargetLocation(context.Background(), targetLocationRequest(request), peer.binding); !errors.Is(err, ErrWorkerTLSRequired) {
			t.Fatalf("incomplete TLS client accepted an existence read: %v", err)
		}
	}
	client := targetObservationTLSClient(t, peer)
	duplicate := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer "+tlsTestSecret)
	if _, err := client.InspectFilesystemBackupTarget(duplicate, request, peer.binding); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatal("duplicate credential was not rejected")
	}
	if _, err := client.ProbeFilesystemBackupTargetLocation(duplicate, targetLocationRequest(request), peer.binding); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatal("duplicate location credential was not rejected")
	}
	if _, err := client.InspectFilesystemBackupTarget(nil, request, peer.binding); !errors.Is(err, ErrTargetInspectionResponse) {
		t.Fatal("nil context was accepted")
	}
	if _, err := client.ProbeFilesystemBackupTargetLocation(context.Background(), nil, peer.binding); !errors.Is(err, ErrTargetInspectionResponse) {
		t.Fatal("nil request was accepted")
	}
	if err := store.ValidateBackupTargetInspectionRequest(nil); !errors.Is(err, store.ErrTargetRegistrationInvalid) {
		t.Fatal("nil marker request was accepted")
	}
}

func TestTargetInspectionClientRejectsInvalidBindingsBeforeRPC(t *testing.T) {
	peer := newObservationPeer(t)
	client := targetObservationTLSClient(t, peer)
	request := targetObservationRequest(t)
	for _, binding := range []ControlSignerBinding{
		{}, {PublicKey: "invalid", FleetID: "fleet-a", Environment: "test"},
		{PublicKey: peer.binding.PublicKey, Environment: "test"},
		{PublicKey: peer.binding.PublicKey, FleetID: "fleet-a"},
	} {
		if _, err := client.InspectFilesystemBackupTarget(context.Background(), request, binding); !errors.Is(err, ErrTargetInspectionResponse) {
			t.Fatal("invalid binding reached marker RPC", err)
		}
		if _, err := client.ProbeFilesystemBackupTargetLocation(context.Background(), targetLocationRequest(request), binding); !errors.Is(err, ErrTargetInspectionResponse) {
			t.Fatal("invalid binding reached location RPC", err)
		}
	}
	if peer.calls.Load() != 0 {
		t.Fatal("invalid local metadata was sent to worker")
	}
}

func TestTargetInspectionClientBoundsUntrustedResponseSizes(t *testing.T) {
	peer := newObservationPeer(t)
	peer.alterInspection = func(receipt *actionv1.FilesystemBackupTargetInspectionReceipt) {
		receipt.Observation.MarkerJson = make([]byte, store.MaxBackupTargetInspectionReceiptBytes+1)
	}
	peer.alterLocation = func(receipt *actionv1.FilesystemBackupTargetLocationReceipt) {
		receipt.Observation.Environment = strings.Repeat("x", store.MaxBackupTargetLocationReceiptBytes+1)
	}
	client := targetObservationTLSClient(t, peer)
	request := targetObservationRequest(t)
	if _, err := client.InspectFilesystemBackupTarget(context.Background(), request, peer.binding); status.Code(err) != codes.ResourceExhausted {
		t.Fatal("oversized marker response was not bounded", err)
	}
	if _, err := client.ProbeFilesystemBackupTargetLocation(context.Background(), targetLocationRequest(request), peer.binding); status.Code(err) != codes.ResourceExhausted {
		t.Fatal("oversized location response was not bounded", err)
	}
}

func TestTargetInspectionClientBindsFullContainmentAndRejectsDowngrade(t *testing.T) {
	peer := newObservationPeer(t)
	client := targetObservationTLSClient(t, peer)
	request := targetObservationRequest(t)
	request.RequireFullContainment = true
	if _, err := client.InspectFilesystemBackupTarget(context.Background(), request, peer.binding); !errors.Is(err, ErrTargetInspectionResponse) {
		t.Fatal("old worker silently omitted contributor checks", err)
	}
	peer.fullContainment = true
	observed, err := client.InspectFilesystemBackupTarget(context.Background(), request, peer.binding)
	if err != nil || !observed.Observation.FullContainmentChecked {
		t.Fatal("signed complete containment refused", err)
	}
	observed.Observation.FullContainmentChecked = false
	if err := store.VerifyBackupTargetInspectionReceipt(request, observed, observationContext(peer.binding)); err == nil {
		t.Fatal("unsigned downgrade retained a valid signature")
	}
}
