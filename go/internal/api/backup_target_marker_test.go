package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// The isolated TLS peer simulates losing exactly one dispatched reply. Go
// handles a real HTTP request and transaction, using a fixture's effect proof.
type publicTargetMarkerPeer struct {
	actionv1.UnimplementedBackupTargetMarkerWorkerServer
	control          *backupTargetMarkerControl
	signer           string
	result           atomic.Pointer[actionv1.TargetMarkerReceipt]
	initialized      atomic.Int64
	reconciled       atomic.Int64
	observationFault atomic.Int32
}

const (
	markerReadMissing int32 = iota + 1
	markerReadInvalid
	markerDirectoryChanged
	markerCompetingUnavailable
)

func (p *publicTargetMarkerPeer) inspect(observation *actionv1.FilesystemBackupTargetObservation) {
	observation.DirectoryIdentity = make([]byte, 32)
	observation.DirectoryIdentity[0] = 42
	fault := p.observationFault.Load()
	if fault == markerDirectoryChanged {
		observation.DirectoryIdentity[0] = 43
	}
	if receipt := p.result.Load(); receipt != nil && fault != markerReadMissing {
		observation.MarkerPresent = true
		observation.MarkerJson = receipt.Effect.MarkerJson
		observation.MarkerSha256 = receipt.Effect.MarkerSha256
		if fault == markerReadInvalid {
			observation.MarkerJson = []byte("{")
			digest := sha256.Sum256(observation.MarkerJson)
			observation.MarkerSha256 = digest[:]
		}
		return
	}
	observation.MarkerPresent = false
	observation.MarkerJson, observation.MarkerSha256 = nil, nil
}

func (p *publicTargetMarkerPeer) Initialize(ctx context.Context, claim *actionv1.TargetMarkerClaim) (*actionv1.TargetMarkerReceipt, error) {
	if p.initialized.Add(1) != 1 || claim.Fence == nil || len(claim.DirectoryIdentity) != 32 ||
		claim.DirectoryIdentity[0] != 42 {
		return nil, status.Error(codes.FailedPrecondition, "initial marker claim was repeated or rebound")
	}
	marker, err := json.Marshal(map[string]any{
		"schemaVersion": 3, "targetId": claim.TargetId, "targetNonce": strings.Repeat("b", 32),
		"incarnationId": "inc_0123456789abcdef", "ownerInstallationId": "inst_derived",
		"targetGeneration": 0, "latestCommitHash": strings.Repeat("0", 64),
		"createdAt": time.Unix(claim.IssuedAtUnix, 0).UTC().Format(time.RFC3339),
	})
	if err != nil {
		return nil, err
	}
	marshal := proto.MarshalOptions{Deterministic: true}
	claimWire, err := marshal.Marshal(claim)
	if err != nil {
		return nil, err
	}
	preparation, err := marshal.Marshal(&actionv1.TargetMarkerPreparation{
		Claim: claim, DirectoryIdentity: claim.DirectoryIdentity, MarkerJson: marker,
	})
	if err != nil {
		return nil, err
	}
	claimSHA, prepSHA, markerSHA := sha256.Sum256(claimWire), sha256.Sum256(preparation), sha256.Sum256(marker)
	permit, err := p.control.BeginDispatch(ctx, &actionv1.TargetMarkerDispatchRequest{
		Fence: claim.Fence, Claim: claim, PreparationSha256: prepSHA[:],
	})
	if err != nil || !bytes.Equal(permit.GetPreparationSha256(), prepSHA[:]) || permit.GetLeaseUntilUnix() != claim.LeaseUntilUnix {
		return nil, status.Error(codes.FailedPrecondition, "Go refused original preparation")
	}
	effect := &actionv1.TargetMarkerEffect{
		SchemaVersion: 1, Fence: claim.Fence, OperationId: claim.OperationId, ClaimSha256: claimSHA[:],
		PreparationSha256: prepSHA[:], CanonicalPath: claim.CanonicalPath, DirectoryIdentity: claim.DirectoryIdentity,
		MarkerJson: marker, MarkerSha256: markerSHA[:],
	}
	wire, err := marshal.Marshal(effect)
	if err != nil {
		return nil, err
	}
	p.result.Store(&actionv1.TargetMarkerReceipt{Effect: effect, SignerPublicKey: p.signer,
		Signature: ed25519.Sign(promotionRoutePrivate, append([]byte("deepseek-infra:backup-target-marker-effect-v1\x00"), wire...))})
	return nil, status.Error(codes.Unavailable, "isolated effect reply lost")
}

func (p *publicTargetMarkerPeer) Reconcile(_ context.Context, input *actionv1.ReconcileTargetMarkerRequest) (*actionv1.TargetMarkerReceipt, error) {
	p.reconciled.Add(1)
	receipt := p.result.Load()
	if receipt == nil || input.SchemaVersion != 1 || input.CurrentWriterFencingToken == 0 ||
		input.OperationId != receipt.Effect.OperationId || !proto.Equal(input.Fence, receipt.Effect.Fence) ||
		!bytes.Equal(input.ClaimSha256, receipt.Effect.ClaimSha256) {
		return nil, status.Error(codes.FailedPrecondition, "original effect fence is missing")
	}
	return receipt, nil
}

func TestTargetRegistrationMarkerHTTPRecoversOriginalRequestAfterEffectReplyLoss(t *testing.T) {
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	control, peer, server, _ := publicTargetTLSFixture(t, "marker-reply-lost")
	const endpoint = "/api/workspace/backup-targets"
	const operation = store.PublicTargetOperationPrefix + "http-marker-recovery"
	base := t.TempDir()
	path := filepath.Join(base, "marker-subject")
	competing, _ := json.Marshal(map[string]any{"path": filepath.Join(base, "competing")})
	if code, response, _ := publicTargetHTTPRequest(t, server, http.MethodPost, endpoint, string(competing),
		store.PublicTargetOperationPrefix+"http-prior-target"); code != http.StatusOK {
		t.Fatalf("could not register competing target fixture: HTTP %d %s", code, response)
	}
	badMetadata, _ := json.Marshal(map[string]any{"path": path, "maxConcurrentTransfers": -1})
	if code, response, _ := publicTargetHTTPRequest(t, server, http.MethodPost, endpoint, string(badMetadata), operation); code != http.StatusBadRequest || !bytes.Contains(response, []byte("invalid_payload")) || peer.marker.initialized.Load() != 0 {
		t.Fatalf("invalid metadata started target initialization: HTTP %d %s", code, response)
	}
	request, _ := json.Marshal(map[string]any{"path": path, "label": "Original request"})
	invoke := func(body []byte) (int, []byte, string) {
		t.Helper()
		return publicTargetHTTPRequest(t, server, http.MethodPost, endpoint, string(body), operation)
	}
	digest := sha256.Sum256(request)
	peer.marker.observationFault.Store(markerCompetingUnavailable)
	if code, _, _ := invoke(request); code != http.StatusServiceUnavailable || peer.marker.initialized.Load() != 0 {
		t.Fatal("a competing-read failure admitted an effect", code)
	}
	peer.marker.observationFault.Store(0)
	code, response, header := invoke(request)
	if code != http.StatusServiceUnavailable || header != operation || !bytes.Contains(response, []byte("BACKUP_TARGET_MARKER_EFFECT_UNKNOWN")) {
		t.Fatalf("lost effect reply did not expose recoverable original operation: HTTP %d %s", code, response)
	}
	pending, found, err := control.PendingTargetMarker(operation, publicTargetActor, hex.EncodeToString(digest[:]))
	if err != nil || !found || pending.State != "DISPATCHED" || peer.marker.initialized.Load() != 1 {
		t.Fatal("lost reply did not retain original dispatched action", found, err)
	}
	if _, exists, err := control.Get("target", pending.Claim.TargetId); exists || err != nil {
		t.Fatal("uncertain marker appeared as a registered target", exists, err)
	}
	changed, _ := json.Marshal(map[string]any{"path": path, "label": "Changed"})
	if code, _, _ := invoke(changed); code != http.StatusConflict || peer.marker.reconciled.Load() != 0 {
		t.Fatal("changed request was allowed to reconcile", code)
	}
	for _, fault := range []int32{markerDirectoryChanged, markerReadMissing, markerReadInvalid, markerCompetingUnavailable} {
		peer.marker.observationFault.Store(fault)
		if code, response, _ := invoke(request); code != http.StatusServiceUnavailable ||
			!bytes.Contains(response, []byte("BACKUP_TARGET_MARKER_EFFECT_UNKNOWN")) {
			t.Fatal("invalid fresh observation settled original effect", fault, code, string(response))
		}
	}
	peer.marker.observationFault.Store(0)
	code, settled, header := invoke(request)
	if code != http.StatusOK || header != operation || !bytes.Contains(settled, []byte(pending.Claim.TargetId)) ||
		!bytes.Contains(settled, []byte("Original request")) || bytes.Contains(settled, []byte("_nativeTargetRegistrationV1")) {
		t.Fatalf("original request could not settle: HTTP %d %s", code, settled)
	}
	before := peer.calls.Load()
	if code, replay, header := invoke(request); code != http.StatusOK || header != operation || !bytes.Equal(replay, settled) ||
		peer.calls.Load() != before || peer.marker.initialized.Load() != 1 || peer.marker.reconciled.Load() != 5 {
		t.Fatal("HTTP replay redispatched or reinterpreted original result", code)
	}
}

func TestTargetRegistrationMarkerControlRequiresNativeAuthorityAndLiveRequest(t *testing.T) {
	control := targetPublicFixtureStoreClock(t, true, true, func() int64 { return time.Now().Unix() })
	authority := HealthFor(config.Config{Mode: config.ModeAuthoritative})
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	for _, service := range []*backupTargetMarkerControl{{health: authority}, {control: control, health: ShadowHealth()}} {
		if _, err := service.BeginDispatch(context.Background(), nil); status.Code(err) != codes.PermissionDenied {
			t.Fatal("control marker RPC accepted a shadow or missing store", err)
		}
	}
	service := &backupTargetMarkerControl{control: control, health: authority}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := service.BeginDispatch(ctx, nil); status.Code(err) != codes.Canceled {
		t.Fatal("control marker RPC accepted a canceled request", err)
	}
	if _, err := service.BeginDispatch(context.Background(), &actionv1.TargetMarkerDispatchRequest{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatal("control marker RPC accepted an unbound claim", err)
	}
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "hybrid")
	if _, err := service.BeginDispatch(context.Background(), nil); status.Code(err) != codes.PermissionDenied {
		t.Fatal("control marker RPC accepted non-native runtime", err)
	}
}
