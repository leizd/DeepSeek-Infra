package api

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
)

type mediaPatchBrokenBody struct{}

func (mediaPatchBrokenBody) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }

func TestMediaPublicPatchBodyFailureAndCrossMethodReceiptRemainClosed(t *testing.T) {
	request := httptest.NewRequest(http.MethodPatch, "/api/media/media_one", nil)
	request.ContentLength, request.Body = 1, io.NopCloser(mediaPatchBrokenBody{})
	response := httptest.NewRecorder()
	if patch, accepted := parseMediaPublicPatch(response, request); accepted || patch != nil {
		t.Fatal("an unreadable body was admitted")
	}
	if response.Code != http.StatusBadRequest || !strings.Contains(response.Body.String(), "Invalid JSON body") {
		t.Fatal("body I/O failure lost its public error", response.Code, response.Body.String())
	}
	response = httptest.NewRecorder()
	outcome := &mediav1.MediaPatchOutcome{SchemaVersion: mediamodel.FlatPatchOutcomeVersion}
	writeMediaPatchOutcome(response, httptest.NewRequest(http.MethodDelete, "/api/media/media_one", nil), nil, outcome)
	if response.Code != http.StatusMethodNotAllowed || response.Body.Len() != 0 {
		t.Fatal("flat PATCH result was emitted for another method", response.Code)
	}
}
