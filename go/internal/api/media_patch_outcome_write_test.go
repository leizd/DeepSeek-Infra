package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
)

// writeMediaOutcome turns a settled media outcome into the public response. It is
// the last place a mistake turns into a wrong HTTP body, and its refusal branches
// are the ones that must never answer "ok": an outcome that cannot be represented
// has to become EFFECT_UNKNOWN so the client reconciles instead of retrying a
// write whose effect may already exist.

func TestWriteMediaOutcomeMapsRefusalsToTheSignedStatus(t *testing.T) {
	outcome := &mediav1.MediaPatchOutcome{
		OperationId:  "media-patch-refused",
		State:        mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED,
		HttpStatus:   409,
		ErrorCode:    "conflict",
		ErrorMessage: "Media is locked",
	}
	request := httptest.NewRequest(http.MethodPatch, "/api/media/media_one", nil)
	recorder := httptest.NewRecorder()
	writeMediaOutcome(recorder, request, outcome)
	if recorder.Code != 409 {
		t.Fatalf("status %d, want 409", recorder.Code)
	}
	if !strings.Contains(recorder.Body.String(), "conflict") {
		t.Fatalf("body lost the original code: %s", recorder.Body.String())
	}
	if recorder.Header().Get("X-DeepSeek-Operation-Id") != outcome.OperationId {
		t.Fatal("operation id header missing")
	}
}

func TestWriteMediaOutcomeTreatsAServerErrorSchemaAsARefusal(t *testing.T) {
	// A signed 500 outcome is a durable refusal, not a success with an empty body.
	outcome := &mediav1.MediaPatchOutcome{
		OperationId:   "media-patch-server-error",
		SchemaVersion: 3,
		State:         mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
		HttpStatus:    500,
		ErrorCode:     "internal",
		ErrorMessage:  "Server error",
		Media:         &mediav1.MediaObject{},
	}
	request := httptest.NewRequest(http.MethodPatch, "/api/media/media_one", nil)
	recorder := httptest.NewRecorder()
	writeMediaOutcome(recorder, request, outcome)
	if recorder.Code != 500 {
		t.Fatalf("status %d, want 500", recorder.Code)
	}
	if strings.Contains(recorder.Body.String(), `"ok":true`) {
		t.Fatalf("a server-error outcome answered success: %s", recorder.Body.String())
	}
}

func TestWriteMediaOutcomeAnswersDeleteCounts(t *testing.T) {
	request := httptest.NewRequest(http.MethodDelete, "/api/media/media_one", nil)
	for count, want := range map[uint32]string{0: `"deleted":0`, 1: `"deleted":1`} {
		outcome := &mediav1.MediaPatchOutcome{
			OperationId:  "media-delete-count",
			State:        mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
			HttpStatus:   200,
			DeletedCount: count,
		}
		recorder := httptest.NewRecorder()
		writeMediaOutcome(recorder, request, outcome)
		if recorder.Code != 200 {
			t.Fatalf("count %d: status %d", count, recorder.Code)
		}
		if !strings.Contains(recorder.Body.String(), want) {
			t.Fatalf("count %d: body %s", count, recorder.Body.String())
		}
	}
}

func TestWriteMediaOutcomeRefusesAnImpossibleDelete(t *testing.T) {
	// A delete outcome that carries media or claims more than one deletion cannot
	// be true; answering EFFECT_UNKNOWN is what stops the client from assuming the
	// write did not happen.
	for name, outcome := range map[string]*mediav1.MediaPatchOutcome{
		"carried-media": {
			OperationId: "media-delete-media", State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
			HttpStatus: 200, Media: &mediav1.MediaObject{},
		},
		"too-many": {
			OperationId: "media-delete-two", State: mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
			HttpStatus: 200, DeletedCount: 2,
		},
	} {
		request := httptest.NewRequest(http.MethodDelete, "/api/media/media_one", nil)
		recorder := httptest.NewRecorder()
		writeMediaOutcome(recorder, request, outcome)
		if recorder.Code != 503 || !strings.Contains(recorder.Body.String(), "EFFECT_UNKNOWN") {
			t.Fatalf("%s: got %d %s", name, recorder.Code, recorder.Body.String())
		}
	}
}

func TestWriteMediaOutcomeRefusesAnUnrepresentablePatchRecord(t *testing.T) {
	// A malformed media record cannot be serialized, so it must become
	// EFFECT_UNKNOWN rather than a partial body.
	outcome := &mediav1.MediaPatchOutcome{
		OperationId: "media-patch-unrepresentable",
		State:       mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
		HttpStatus:  200,
		Media:       &mediav1.MediaObject{Members: []*mediav1.MediaMember{{Key: "invalid", Value: nil}}},
	}
	request := httptest.NewRequest(http.MethodPatch, "/api/media/media_one", nil)
	recorder := httptest.NewRecorder()
	writeMediaOutcome(recorder, request, outcome)
	if recorder.Code != 503 || !strings.Contains(recorder.Body.String(), "EFFECT_UNKNOWN") {
		t.Fatalf("got %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestWriteMediaPatchOutcomeRefusesAFlatRecordOnANonPatchMethod(t *testing.T) {
	// A flat record is only meaningful for PATCH; answering it to a DELETE would
	// hand the wrong shape to the caller.
	outcome := &mediav1.MediaPatchOutcome{
		OperationId: "media-patch-flat-wrong-method",
		// Flat records are outcome schema 4; the version is what selects the
		// streaming path, so it has to match the constant rather than a guess.
		SchemaVersion: mediamodel.FlatPatchOutcomeVersion,
		State:         mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED,
		HttpStatus:    200,
	}
	request := httptest.NewRequest(http.MethodDelete, "/api/media/media_one", nil)
	recorder := httptest.NewRecorder()
	// A nil client is safe here: the method guard runs before any worker call.
	writeMediaPatchOutcome(recorder, request, nil, outcome)
	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status %d, want 405", recorder.Code)
	}
}

func TestWriteMediaPatchOutcomeFallsBackForAnUnsupportedSchema(t *testing.T) {
	// An outcome that negotiates no streaming schema is written through the plain
	// path, which is how a refusal reaches the client without a worker read.
	outcome := &mediav1.MediaPatchOutcome{
		OperationId:   "media-patch-unsupported-schema",
		SchemaVersion: 1,
		State:         mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED,
		HttpStatus:    409,
		ErrorCode:     "conflict",
		ErrorMessage:  "Media is locked",
	}
	request := httptest.NewRequest(http.MethodPatch, "/api/media/media_one", nil)
	recorder := httptest.NewRecorder()
	writeMediaPatchOutcome(recorder, request, nil, outcome)
	if recorder.Code != 409 {
		t.Fatalf("status %d, want 409 (%s)", recorder.Code, recorder.Body.String())
	}
}
