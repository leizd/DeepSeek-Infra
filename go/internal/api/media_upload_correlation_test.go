package api

import (
	"strconv"
	"strings"
	"testing"
)

// mediaUploadCorrelationID mirrors the Python derive_ocr_correlation_id rule: a
// single upload keeps the request id, a multi-file upload derives one bounded id
// per object. The truncation arm decides whether two objects can collide.

func TestMediaUploadCorrelationIDKeepsSingleUploadsUnchanged(t *testing.T) {
	for _, total := range []int{0, 1, -1} {
		if got := mediaUploadCorrelationID("request-id", 3, total); got != "request-id" {
			t.Fatalf("total=%d derived %q", total, got)
		}
	}
	// An empty base stays empty for a single upload rather than inventing an id.
	if got := mediaUploadCorrelationID("", 0, 1); got != "" {
		t.Fatalf("empty base derived %q", got)
	}
}

func TestMediaUploadCorrelationIDSuffixesMultiFileUploads(t *testing.T) {
	if got := mediaUploadCorrelationID("request-id", 0, 3); got != "request-id.0" {
		t.Fatalf("ordinal 0 derived %q", got)
	}
	if got := mediaUploadCorrelationID("request-id", 2, 3); got != "request-id.2" {
		t.Fatalf("ordinal 2 derived %q", got)
	}
	// Distinct ordinals must not collide.
	seen := map[string]bool{}
	for ordinal := 0; ordinal < 10; ordinal++ {
		id := mediaUploadCorrelationID("request-id", ordinal, 10)
		if seen[id] {
			t.Fatalf("ordinal %d collided on %q", ordinal, id)
		}
		seen[id] = true
	}
}

func TestMediaUploadCorrelationIDBoundsTheResult(t *testing.T) {
	// A long base is truncated so the suffixed id still fits the 64-byte budget.
	long := strings.Repeat("a", 200)
	for _, ordinal := range []int{0, 9, 99} {
		id := mediaUploadCorrelationID(long, ordinal, 100)
		if len(id) > 64 {
			t.Fatalf("ordinal %d produced %d bytes: %q", ordinal, len(id), id)
		}
		if !strings.HasSuffix(id, "."+strconv.Itoa(ordinal)) {
			t.Fatalf("ordinal %d lost its suffix: %q", ordinal, id)
		}
	}
	// Exactly-fitting bases are not truncated.
	fit := strings.Repeat("b", 62)
	if got := mediaUploadCorrelationID(fit, 1, 2); got != fit+".1" {
		t.Fatalf("a fitting base was altered: %q", got)
	}
}
