package worker

import (
	"context"
	"errors"
	"testing"
)

func TestValidateTLSTargetRefusesUnusablePorts(t *testing.T) {
	for _, target := range []string{"host:notaport", "host:0", "host:70000", "host:", ":50052", "host"} {
		if err := validateTLSTarget(target); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
			t.Fatalf("target %q was accepted: %v", target, err)
		}
	}
	if err := validateTLSTarget("127.0.0.1:50052"); err != nil {
		t.Fatalf("valid target refused: %v", err)
	}
}

func TestOutgoingContextRefusesANilContext(t *testing.T) {
	client := &Client{}
	if _, err := client.outgoingContext(nil, ""); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatalf("nil context accepted: %v", err)
	}
	if _, err := client.outgoingContext(context.Background(), ""); err != nil {
		t.Fatalf("plain background context refused: %v", err)
	}
}
