package action

import (
	"context"
	"errors"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// genesisAuthority builds a valid control-authority-v1 genesis checkpoint through
// the exported digest helpers, so a test does not need the frozen corpus.
func genesisAuthority(t *testing.T) *store.AuthorityCheckpoint {
	t.Helper()
	checkpoint := &store.AuthorityCheckpoint{
		Schema:                     store.ControlAuthoritySchema,
		AuthorityGeneration:        1,
		CreatedAt:                  "2026-09-27T00:00:00Z",
		ControlSchemaVersion:       7,
		Policies:                   []any{},
		Targets:                    []any{},
		ReceiptMutationGenerations: map[string]int64{},
		PromotionEpochs:            map[string]int64{},
		DrainGenerations:           map[string]int64{},
		PlacementGenerations:       map[string]int64{},
	}
	payloadDigest, err := store.ComputePayloadDigest(checkpoint)
	if err != nil {
		t.Fatal(err)
	}
	checkpoint.PayloadDigest = payloadDigest
	digest, err := store.ComputeCheckpointDigest(checkpoint)
	if err != nil {
		t.Fatal(err)
	}
	checkpoint.Digest = digest
	return checkpoint
}

// promotedActionDomain is the only way this package's production path can be
// reached: a deployment that opted in, a claimed control authority, and a durable
// cutover of the action domain to go_authoritative.
func promotedActionDomain(t *testing.T) *store.Control {
	t.Helper()
	control, err := store.OpenControl(store.OpenOptions{
		Path:             t.TempDir(),
		Owner:            "production-owner",
		Now:              func() int64 { return 500 },
		AuthorizeCutover: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	authority := genesisAuthority(t)
	if _, advanced, err := control.ClaimControlAuthority(authority); err != nil || !advanced {
		t.Fatalf("claim control authority: advanced=%v %v", advanced, err)
	}
	for _, to := range []store.CutoverState{store.CutoverDualEvaluate, store.CutoverGoAuthoritative} {
		current, err := control.GetCutover(authorityDomain)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.TransitionCutover(store.CutoverTransition{
			Domain:           authorityDomain,
			To:               to,
			ExpectedRevision: current.Revision,
			ExpectedEpoch:    current.Epoch,
			FencingToken:     current.FencingToken,
			TransferID:       "action-promote-" + string(to),
			Authority:        authority,
		}); err != nil {
			t.Fatalf("promote %s: %v", to, err)
		}
	}
	return control
}

// A caller's flag is a claim, not authority: the gate must consult the durable
// cutover record, and the claim alone must never unlock the production path.
func TestProductionAuthorityIsDurableAndNotSelfAsserted(t *testing.T) {
	t.Run("claims without a cutover record", func(t *testing.T) {
		st := newFakeStore()
		coordinator := NewCoordinator(st, &fakeWorkerClient{}, WithAuthoritative(true))
		if _, err := coordinator.ExecuteStorageAction(context.Background(), "a", storageRequest("op")); !errors.Is(err, store.ErrCutoverNotAuthorized) {
			t.Fatalf("self-asserted authority was accepted: %v", err)
		}
		if st.authorityDomain != authorityDomain {
			t.Fatalf("gate consulted %q, want %q", st.authorityDomain, authorityDomain)
		}
	})

	t.Run("claim backed by the durable record", func(t *testing.T) {
		st := newFakeStore()
		st.authoritative = true
		coordinator := NewCoordinator(st, &fakeWorkerClient{}, WithAuthoritative(true))
		if _, err := coordinator.ExecuteStorageAction(context.Background(), "a", storageRequest("op")); !errors.Is(err, ErrActionNotFound) {
			t.Fatalf("promoted domain did not pass the gate: %v", err)
		}
	})

	t.Run("durable read failure propagates", func(t *testing.T) {
		st := newFakeStore()
		st.authorityErr = errAuthorityProbe
		coordinator := NewCoordinator(st, &fakeWorkerClient{}, WithAuthoritative(true))
		if _, err := coordinator.ExecuteStorageAction(context.Background(), "a", storageRequest("op")); !errors.Is(err, errAuthorityProbe) {
			t.Fatalf("durable read failure was swallowed: %v", err)
		}
	})

	t.Run("non-authoritative path is unchanged", func(t *testing.T) {
		st := newFakeStore()
		coordinator := NewCoordinator(st, &fakeWorkerClient{}, WithNow(func() int64 { return 500 }))
		if _, err := coordinator.ExecuteStorageAction(context.Background(), "a", storageRequest("op")); !errors.Is(err, ErrActionNotFound) {
			t.Fatalf("qualification path changed: %v", err)
		}
		if st.authorityDomain != "" {
			t.Fatalf("qualification path consulted the cutover record: %q", st.authorityDomain)
		}
	})
}

var errAuthorityProbe = errors.New("authority read unavailable")

// The gate itself refuses without a store, so a half-built coordinator cannot
// reach the execution path with production authority and no way to prove it.
func TestAssertProductionAuthorityRefusesWithoutAStore(t *testing.T) {
	coordinator := &Coordinator{authoritative: true}
	if err := coordinator.assertProductionAuthority(); !errors.Is(err, internalprotocol.ErrUnknownEffect) {
		t.Fatalf("nil store: %v", err)
	}
	if err := (&Coordinator{}).assertProductionAuthority(); err != nil {
		t.Fatalf("non-authoritative coordinator: %v", err)
	}
}

// The legal path must genuinely succeed: with the action domain durably promoted,
// a coordinator that claims production authority dispatches and reaches a
// terminal SUCCEEDED record carrying the provider effect identity.
func TestProductionExecutionRunsOnceTheActionDomainIsPromoted(t *testing.T) {
	control := promotedActionDomain(t)
	pendingStorageAction(t, control)
	worker := &inspectingWorker{execute: func(request *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
		if request.Fence == nil || request.Fence.ActionId != "durable" || request.Fence.ExecutionEpoch != 1 {
			t.Errorf("dispatch lost its fence: %+v", request.Fence)
		}
		return &actionv1.StorageMutationResponse{
			Fence:       request.Fence,
			OperationId: request.OperationId,
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			State:       commonv1.EffectState_EFFECT_STATE_APPLIED,
			Etag:        "etag-1",
			EffectId:    "effect-1",
		}, nil
	}}
	coordinator := NewCoordinator(control, worker, WithNow(func() int64 { return 500 }), WithAuthoritative(true))
	response, err := coordinator.ExecuteStorageAction(context.Background(), "durable", storageRequest("promoted-operation"))
	if err != nil {
		t.Fatalf("promoted production execution: %v", err)
	}
	if response == nil || response.EffectId != "effect-1" {
		t.Fatalf("provider effect identity was not returned: %+v", response)
	}
	record, exists, err := control.Get("action", "durable")
	if err != nil || !exists {
		t.Fatalf("action record: exists=%v %v", exists, err)
	}
	if record.State != "SUCCEEDED" {
		t.Fatalf("production execution did not reach a terminal state: %+v", record)
	}
	intent, bound, err := control.GetStorageDispatch("durable", 1)
	if err != nil || !bound || intent.Intent.OperationID != "promoted-operation" {
		t.Fatalf("production dispatch was not durably bound: bound=%v %v", bound, err)
	}
}

// Every production entry point must refuse while the domain is still Python's,
// and must refuse before it touches any durable state.
func TestProductionAuthorityRefusesEveryEntryPointWhileUnpromoted(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{
		Path:             t.TempDir(),
		Owner:            "shadow-owner",
		Now:              func() int64 { return 500 },
		AuthorizeCutover: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	coordinator := NewCoordinator(control, &fakeWorkerClient{
		onExecute: func() { t.Error("refused production path reached the worker") },
		onQuery:   func() { t.Error("refused production path queried the worker") },
	}, WithNow(func() int64 { return 500 }), WithAuthoritative(true))

	if _, err := coordinator.ExecuteStorageAction(context.Background(), "durable", storageRequest("op")); !errors.Is(err, store.ErrCutoverNotAuthorized) {
		t.Fatalf("ExecuteStorageAction: %v", err)
	}
	if _, err := coordinator.ReconcileStorageAction(context.Background(), "durable", "op"); !errors.Is(err, store.ErrCutoverNotAuthorized) {
		t.Fatalf("ReconcileStorageAction: %v", err)
	}
	if _, err := coordinator.ReconcileClaimedStorageAction(context.Background(), store.ActionLease{ActionID: "durable", Epoch: 1}, "op"); !errors.Is(err, store.ErrCutoverNotAuthorized) {
		t.Fatalf("ReconcileClaimedStorageAction: %v", err)
	}
	if _, err := coordinator.ExecuteClaimedStorageAction(context.Background(), store.ActionLease{ActionID: "durable", Epoch: 1}, storageRequest("op")); !errors.Is(err, store.ErrCutoverNotAuthorized) {
		t.Fatalf("ExecuteClaimedStorageAction: %v", err)
	}
	if _, exists, err := control.Get("action", "durable"); err != nil || exists {
		t.Fatalf("refused production path wrote durable state: exists=%v %v", exists, err)
	}
	// The authority itself is unchanged: no refusal may advance a cutover.
	record, err := control.GetCutover(authorityDomain)
	if err != nil || record.State != store.CutoverShadow || record.Epoch != 1 {
		t.Fatalf("refusal advanced the cutover: %+v %v", record, err)
	}
}

func TestIsGoAuthoritativeReadsTheDurableRecord(t *testing.T) {
	control := promotedActionDomain(t)
	if authoritative, err := control.IsGoAuthoritative(authorityDomain); err != nil || !authoritative {
		t.Fatalf("promoted domain: %v %v", authoritative, err)
	}
	if authoritative, err := control.IsGoAuthoritative("policy"); err != nil || authoritative {
		t.Fatalf("shadow domain: %v %v", authoritative, err)
	}
	if _, err := control.IsGoAuthoritative("nonexistent"); !errors.Is(err, store.ErrUnknownDomain) {
		t.Fatalf("unknown domain: %v", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.IsGoAuthoritative(authorityDomain); !errors.Is(err, store.ErrWriterFenceHeld) {
		t.Fatalf("closed store: %v", err)
	}
	if authoritative, err := control.IsGoAuthoritative("policy"); !errors.Is(err, store.ErrWriterFenceHeld) || authoritative {
		t.Fatalf("closed store must not report authority: %v %v", authoritative, err)
	}
}
