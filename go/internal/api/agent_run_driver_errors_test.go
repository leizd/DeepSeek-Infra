package api

import (
	"context"
	"errors"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
)

// driverErrorCode is the only place the driver's internal sentinels become the
// codes a controller observes. A missing arm turns a precise refusal into a
// generic transport error, so every branch is pinned here.

func TestDriverErrorCodeMapsEverySentinel(t *testing.T) {
	cases := []struct {
		err   error
		code  string
		label string
	}{
		{errAgentDriverUnknown, "AGENT_DRIVER_EFFECT_UNCONFIRMED", "unknown"},
		{errAgentDriverTools, "AGENT_DRIVER_TOOLS_PENDING", "tools"},
		{errAgentDriverReset, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "reset"},
		{errAgentDriverHeld, "AGENT_RUNTIME_SUCCESSOR_HELD", "held"},
		{errAgentDriverStale, "STALE_EXECUTION_EPOCH", "stale"},
		{errAgentDriverFence, "FENCE_MISMATCH", "fence"},
		{context.Canceled, "AGENT_DRIVER_CANCELLED", "cancelled"},
		{worker.ErrControlSignerResponse, "AGENT_DRIVER_SIGNER_BINDING_INVALID", "signer"},
		{worker.ErrWorkerTLSConfigInvalid, "AGENT_DRIVER_WORKER_TRANSPORT_INVALID", "tls"},
	}
	for _, row := range cases {
		if got := driverErrorCode(row.err); got != row.code {
			t.Fatalf("%s: got %q, want %q", row.label, got, row.code)
		}
	}

	// A wrapped sentinel still maps, because the caller may add context.
	if got := driverErrorCode(errors.Join(errAgentDriverHeld, errors.New("detail"))); got != "AGENT_RUNTIME_SUCCESSOR_HELD" {
		t.Fatalf("a wrapped sentinel mapped to %q", got)
	}

	// A typed successor refusal reports its own code.
	refused := &store.AgentSuccessorError{Code: "AGENT_RUNTIME_GENERATION_CONFLICT"}
	if got := driverErrorCode(refused); got != "AGENT_RUNTIME_GENERATION_CONFLICT" {
		t.Fatalf("a successor refusal mapped to %q", got)
	}

	// A successor error with no code, and any unrecognised error, fall back to
	// the authority/transport code rather than an empty string.
	if got := driverErrorCode(&store.AgentSuccessorError{}); got != "AGENT_DRIVER_AUTHORITY_OR_TRANSPORT_UNAVAILABLE" {
		t.Fatalf("an empty successor code mapped to %q", got)
	}
	if got := driverErrorCode(errors.New("something else")); got != "AGENT_DRIVER_AUTHORITY_OR_TRANSPORT_UNAVAILABLE" {
		t.Fatalf("an unknown error mapped to %q", got)
	}
	// A nil error is not a panic and still resolves to a usable code.
	if got := driverErrorCode(nil); got == "" {
		t.Fatal("a nil error produced no code")
	}
}
