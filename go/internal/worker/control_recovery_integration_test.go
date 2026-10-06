//go:build integration

package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/action"
	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/protobuf/proto"
)

// The gate withholds a real provider's PUT response, or stops before forwarding
// the PUT until after takeover. It never fabricates provider observations,
// objects, or journal rows.
type isolatedProviderFault struct {
	server       *httptest.Server
	committed    bool
	versioned    bool
	reached      chan struct{}
	unblock      chan struct{}
	forward      chan struct{}
	forwarded    chan struct{}
	abort        chan struct{}
	once         sync.Once
	puts         atomic.Int64
	providerPuts atomic.Int64
	heads        atomic.Int64
	gets         atomic.Int64
	putStatus    atomic.Int64
}

func newIsolatedProviderFault(t *testing.T, endpoint string, committed bool) *isolatedProviderFault {
	t.Helper()
	backend, err := url.Parse(endpoint)
	if err != nil || backend.Scheme != "http" || backend.Hostname() != "127.0.0.1" || backend.Path != "" || backend.User != nil {
		t.Fatal("fault gate requires an explicit isolated loopback provider")
	}
	gate := &isolatedProviderFault{committed: committed, reached: make(chan struct{}), unblock: make(chan struct{}),
		forward: make(chan struct{}), forwarded: make(chan struct{}), abort: make(chan struct{})}
	transport := &http.Transport{Proxy: nil}
	t.Cleanup(transport.CloseIdleConnections)
	gate.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, incoming *http.Request) {
		put := incoming.Method == http.MethodPut
		outbound := incoming.Clone(incoming.Context())
		if put {
			if gate.puts.Add(1) != 1 {
				http.Error(w, "duplicate PUT refused by qualification gate", http.StatusConflict)
				return
			}
			if !committed {
				body, err := io.ReadAll(io.LimitReader(incoming.Body, 1024*1024+1))
				if err != nil || len(body) > 1024*1024 {
					http.Error(w, "request body interrupted", http.StatusBadRequest)
					return
				}
				close(gate.reached)
				select {
				case <-gate.forward:
				case <-gate.abort:
					return
				}
				// An already accepted remote request can outlive its caller. Resume
				// that one original request after the successor observes real absence.
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				outbound = incoming.Clone(ctx)
				outbound.Body = io.NopCloser(bytes.NewReader(body))
			}
		}
		if incoming.Method == http.MethodHead {
			gate.heads.Add(1)
		}
		if incoming.Method == http.MethodGet {
			gate.gets.Add(1)
		}
		location := *incoming.URL
		location.Scheme, location.Host = backend.Scheme, backend.Host
		outbound.URL, outbound.RequestURI = &location, ""
		// Preserve the signed Host while routing bytes to the actual MinIO process.
		outbound.Host = incoming.Host
		if put {
			gate.providerPuts.Add(1)
		}
		response, err := transport.RoundTrip(outbound)
		if err != nil {
			if put {
				gate.putStatus.Store(http.StatusBadGateway)
				close(gate.forwarded)
			}
			http.Error(w, "isolated provider transport failed", http.StatusBadGateway)
			return
		}
		defer response.Body.Close()
		if put {
			gate.putStatus.Store(int64(response.StatusCode))
			close(gate.forwarded)
			if committed {
				close(gate.reached)
				select {
				case <-gate.unblock:
				case <-incoming.Context().Done():
				}
			}
		}
		for key, values := range response.Header {
			for _, value := range values {
				w.Header().Add(key, value)
			}
		}
		w.WriteHeader(response.StatusCode)
		_, _ = io.Copy(w, response.Body)
	}))
	t.Cleanup(gate.server.Close)
	t.Cleanup(gate.release)
	t.Cleanup(func() { close(gate.abort) })
	return gate
}

func (gate *isolatedProviderFault) release() {
	gate.once.Do(func() { close(gate.unblock) })
}

func qualifyLostProviderResponse(t *testing.T, control *store.Control, client *Client, cfg TLSDialConfig,
	template *actionv1.StorageMutationRequest, gate *isolatedProviderFault, now func() int64,
	advance func(int64), stop func(), start func() func(), public string, writerFence int64) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	request := proto.Clone(template).(*actionv1.StorageMutationRequest)
	request.Fence = &commonv1.ActionFence{ActionId: "lost-provider-response-" + randomHex64(t)[:16], ExecutionEpoch: 1}
	request.OperationId, request.RequestId, request.Nonce = randomHex64(t), randomHex64(t), randomHex64(t)
	request.ObjectKey = "lost-response-" + request.Fence.ActionId
	request.TargetIdentity = isolatedS3TargetIdentity(gate.server.URL, request.Bucket, request.Prefix)
	request.CanonicalAuthorization = nil
	if err := control.Put(store.Record{Domain: "action", ID: request.Fence.ActionId, Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: request.Fence.ActionId, LeaseSeconds: 60,
		ResourceKeys: []string{"lost-response-resource-" + request.Fence.ActionId}})
	if err != nil {
		t.Fatal(err)
	}
	coordinator := action.NewCoordinator(control, client, action.WithAuthoritative(true), action.WithNow(now))
	done := make(chan error, 1)
	go func() {
		_, err := coordinator.ExecuteClaimedStorageAction(ctx, claim.Lease, request)
		done <- err
	}()
	select {
	case <-gate.reached:
	case err := <-done:
		t.Fatalf("dispatch ended before fault boundary: %v", err)
	case <-ctx.Done():
		t.Fatal("provider fault boundary timed out")
	}
	if gate.committed && gate.putStatus.Load() != http.StatusOK {
		t.Fatalf("actual provider rejected PUT before interruption: %d", gate.putStatus.Load())
	}
	stop()
	gate.release()
	select {
	case err = <-done:
		if !errors.Is(err, action.ErrStorageMutationUncertain) {
			t.Fatalf("lost response did not remain uncertain: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("killed worker RPC did not finish")
	}
	record, exists, err := control.Get("action", request.Fence.ActionId)
	if err != nil || !exists || record.State != "EFFECT_UNKNOWN" {
		t.Fatalf("unacknowledged effect became settled: state=%s err=%v", record.State, err)
	}
	original, bound, err := control.GetStorageDispatch(request.Fence.ActionId, 1)
	if err != nil || !bound || original.Intent.OperationID != request.OperationId {
		t.Fatalf("original dispatch identity was not retained: %v", err)
	}
	// The production store clock expires only the control lease; the provider
	// and worker use their real clocks and state throughout this qualification.
	lease, found, err := control.GetActionLease(request.Fence.ActionId)
	if err != nil || !found {
		t.Fatalf("original action lease missing: %v", err)
	}
	advance(lease.LeaseUntil + 1)
	if _, err := coordinator.ExecuteClaimedStorageAction(ctx, claim.Lease, request); err == nil {
		t.Fatal("expired claim was allowed to redispatch")
	}
	next, err := control.AdmitAndClaimAction(store.AdmissionRequest{ActionID: request.Fence.ActionId, Owner: "native-recovery-successor", LeaseSeconds: 60})
	if err != nil || next.Lease.Epoch != 2 || next.Record.State != "RECONCILING" {
		t.Fatalf("lease takeover failed: state=%s epoch=%d err=%v", next.Record.State, next.Lease.Epoch, err)
	}
	stopRecovered := start()
	defer stopRecovered()
	recoveredClient, err := DialTLS(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer recoveredClient.Close()
	fence := &commonv1.ActionFence{ActionId: request.Fence.ActionId, ExecutionEpoch: next.Lease.Epoch}
	document, err := recoveredClient.SignControl(ctx, &actionv1.SignControlRequest{
		Purpose: actionv1.ControlSigningPurpose_CONTROL_SIGNING_PURPOSE_INSTALL_EPOCH, Fence: fence,
		RequestId: randomHex64(t), Nonce: randomHex64(t), Revision: next.Record.Revision,
		FencingToken: uint64(writerFence), FleetId: "fleet-a", Environment: "test",
	}, ControlSignerBinding{PublicKey: public, FleetID: "fleet-a", Environment: "test"})
	if err != nil {
		t.Fatal(err)
	}
	if err := recoveredClient.InstallAuthoritativeEpoch(ctx, fence, document); err != nil {
		t.Fatal(err)
	}
	recovery := action.NewCoordinator(control, recoveredClient, action.WithAuthoritative(true), action.WithNow(now))
	response, recoveryErr := recovery.ReconcileClaimedStorageAction(ctx, next.Lease, request.OperationId)
	status, body, etag, version := readIsolatedS3(t, gate.server.URL, request.Bucket, request.Prefix, request.ObjectKey)
	wantState := "EFFECT_UNKNOWN"
	if gate.committed {
		wantState = "VERIFYING"
		if recoveryErr != nil || response == nil || response.State != commonv1.EffectState_EFFECT_STATE_APPLIED ||
			response.Fence.ExecutionEpoch != 1 || response.OperationId != request.OperationId || status != http.StatusOK ||
			!bytes.Equal(body, request.Payload) || response.Etag != etag {
			t.Fatalf("original provider effect was not recovered: response=%v status=%d err=%v", response, status, recoveryErr)
		}
		var metadata map[string]any
		if json.Unmarshal([]byte(response.ProviderMetadata), &metadata) != nil || metadata["bytesVerified"] != true || gate.gets.Load() < 2 {
			t.Fatal("recovery did not conditionally read and verify actual provider bytes")
		}
		// Confirmed replay is read-only, including on the versioned provider.
		for range 2 {
			replayed, err := recoveredClient.QueryStorageEffect(ctx, request.Fence, request.OperationId, "")
			if err != nil || !proto.Equal(replayed, response) {
				t.Fatalf("recovered receipt changed: %v", err)
			}
		}
		_, replayBody, replayETag, replayVersion := readIsolatedS3(t, gate.server.URL, request.Bucket, request.Prefix, request.ObjectKey)
		if !bytes.Equal(replayBody, body) || replayETag != etag || replayVersion != version {
			t.Fatal("recovery replay changed the actual provider object")
		}
	} else if !errors.Is(recoveryErr, action.ErrStorageMutationUncertain) || !errors.Is(recoveryErr, internalprotocol.ErrUnknownEffect) ||
		response == nil || response.State != commonv1.EffectState_EFFECT_STATE_UNKNOWN || status != http.StatusNotFound {
		t.Fatalf("actual provider absence did not remain unknown: status=%d err=%v", status, recoveryErr)
	}
	record, exists, err = control.Get("action", request.Fence.ActionId)
	if err != nil || !exists || record.ExecutionEpoch != 2 || record.State != wantState {
		t.Fatalf("recovery settled the wrong claim: epoch=%d state=%s err=%v", record.ExecutionEpoch, record.State, err)
	}
	unchanged, bound, err := control.GetStorageDispatch(request.Fence.ActionId, 1)
	if err != nil || !bound || unchanged != original {
		t.Fatalf("recovery changed original dispatch: %v", err)
	}
	if _, bound, err := control.GetStorageDispatch(request.Fence.ActionId, 2); err != nil || bound {
		t.Fatalf("takeover minted a new write identity: %v", err)
	}
	if _, err := recovery.ExecuteClaimedStorageAction(ctx, next.Lease, request); err == nil {
		t.Fatal("reconciliation claim was allowed to execute another PUT")
	}
	resources, err := control.GetResourceLeases(request.Fence.ActionId)
	if err != nil || len(resources) != 1 || gate.puts.Load() != 1 || gate.heads.Load() == 0 {
		t.Fatalf("recovery lost reservations or duplicated effects: resources=%d PUT=%d HEAD=%d err=%v", len(resources), gate.puts.Load(), gate.heads.Load(), err)
	}
	if !gate.committed {
		if gate.providerPuts.Load() != 0 {
			t.Fatal("absence was not observed before the original request reached MinIO")
		}
		close(gate.forward)
		select {
		case <-gate.forwarded:
		case <-ctx.Done():
			t.Fatal("delayed original provider request did not finish")
		}
		if gate.putStatus.Load() != http.StatusOK {
			t.Fatalf("late original PUT failed at the actual provider: %d", gate.putStatus.Load())
		}
		response, err = recovery.ReconcileClaimedStorageAction(ctx, next.Lease, request.OperationId)
		status, body, etag, version = readIsolatedS3(t, gate.server.URL, request.Bucket, request.Prefix, request.ObjectKey)
		var metadata map[string]any
		if err != nil || response == nil || response.Fence.ExecutionEpoch != 1 || response.OperationId != request.OperationId ||
			response.State != commonv1.EffectState_EFFECT_STATE_APPLIED || status != http.StatusOK || !bytes.Equal(body, request.Payload) ||
			response.Etag != etag || json.Unmarshal([]byte(response.ProviderMetadata), &metadata) != nil || metadata["bytesVerified"] != true {
			t.Fatalf("late original effect was not conditionally verified: status=%d err=%v", status, err)
		}
		record, exists, err = control.Get("action", request.Fence.ActionId)
		if err != nil || !exists || record.State != "VERIFYING" || record.ExecutionEpoch != 2 {
			t.Fatalf("late original effect did not resume verification: state=%s err=%v", record.State, err)
		}
	}
	if gate.puts.Load() != 1 || gate.providerPuts.Load() != 1 {
		t.Fatal("provider recovery retried the original PUT")
	}
	if gate.versioned && version == "" {
		t.Fatal("recovered versioned provider object has no version")
	}
	stopRecovered()
	stopAgain := start()
	defer stopAgain()
	restarted, err := DialTLS(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	receipt, err := restarted.QueryStorageEffect(ctx, request.Fence, request.OperationId, "")
	if err != nil || !proto.Equal(receipt, response) {
		t.Fatalf("verified recovery receipt did not survive another process death: %v", err)
	}
	status, replayBody, replayETag, replayVersion := readIsolatedS3(t, gate.server.URL, request.Bucket, request.Prefix, request.ObjectKey)
	if status != http.StatusOK || !bytes.Equal(replayBody, body) || replayETag != etag || replayVersion != version || gate.providerPuts.Load() != 1 {
		t.Fatal("restarted recovery changed actual provider bytes or version")
	}
	t.Logf("actual provider response loss: committedBeforeTakeover=%t originalEpoch=1 successorEpoch=2 firstState=%s finalState=VERIFYING workerPUT=1 providerPUT=1 version=%q", gate.committed, wantState, version)
}
