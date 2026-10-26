package api

import (
	"bytes"
	"context"
	"encoding/json"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestMediaPublicPatchKeepsAuthenticationAuthorityAndBodyRefusals(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-api"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	t.Setenv("AUTH_DISABLED", "0")
	t.Setenv("AUTH_TOKEN", strings.Repeat("m", 32))
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	t.Setenv("DEEPSEEK_WORKER_SERVICE_BEARER", "")
	view := PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo}
	cases := []struct {
		name, method, body, token, mode, code string
		status                                int
		length                                int64
	}{
		{name: "auth", method: "PATCH", body: `{"title":"changed"}`, status: 401, code: "unauthorized"},
		{name: "shadow", method: "PATCH", body: `{"title":"changed"}`, token: "valid", mode: "shadow", status: 503, code: "MEDIA_AUTHORITY_UNAVAILABLE"},
		{name: "unsupported", method: "POST", body: `{}`, token: "valid", status: 501, code: "GO_API_NOT_IMPLEMENTED"},
		{name: "empty", method: "PATCH", token: "valid", status: 400, code: "invalid_payload"},
		{name: "oversize", method: "PATCH", body: `{}`, token: "valid", status: 413, code: "upload_too_large", length: 1_000_001},
		{name: "json", method: "PATCH", body: `{`, token: "valid", status: 400, code: "invalid_payload"},
		{name: "trailing", method: "PATCH", body: `{} {}`, token: "valid", status: 400, code: "invalid_payload"},
		{name: "array", method: "PATCH", body: `[]`, token: "valid", status: 400, code: "invalid_payload"},
		{name: "no-fields", method: "PATCH", body: `{"status":"ready"}`, token: "valid", status: 400, code: "invalid_payload"},
		{name: "worker-missing", method: "PATCH", body: `{"title":"changed"}`, token: "valid", status: 503, code: "MEDIA_WORKER_UNAVAILABLE"},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(test.method, "http://127.0.0.1/api/media/media_one", bytes.NewBufferString(test.body))
			if test.token != "" {
				request.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
			}
			if test.length != 0 {
				request.ContentLength = test.length
			}
			v := view
			if test.mode != "" {
				v.Mode = test.mode
			}
			w := httptest.NewRecorder()
			mediaPatch(w, request, control, v)
			if w.Code != test.status {
				t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
			}
			var result map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || result["code"] != test.code {
				t.Fatalf("refusal=%s %v", w.Body.String(), err)
			}
		})
	}
}
func TestMediaRPCDoesNotAcceptAnUnauthoritativeOrUnboundRequest(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-rpc"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	service := &mediaMutationService{control: control, health: ShadowHealth()}
	if _, err := service.GetClaim(context.Background(), &mediav1.GetMediaPatchClaimRequest{}); err == nil {
		t.Fatal("shadow mutation admitted")
	}
	service.health = HealthFor(config.Config{Mode: config.ModeAuthoritative})
	if _, err := service.GetClaim(context.Background(), nil); err == nil {
		t.Fatal("nil request admitted")
	}
	text := &mediav1.MediaValue{Kind: &mediav1.MediaValue_TextValue{TextValue: "changed"}}
	claim, err := control.AdmitMediaPatch(&mediav1.MediaPatchIntent{SchemaVersion: 1, OperationId: "media-patch-rpc", MediaId: "media_one", Patch: &mediav1.MediaPatch{Title: text}})
	if err != nil {
		t.Fatal(err)
	}
	actual, err := service.GetClaim(context.Background(), &mediav1.GetMediaPatchClaimRequest{SchemaVersion: 1, OperationId: "media-patch-rpc", Fence: claim.Fence})
	if err != nil || actual.Fence.ExecutionEpoch != 1 {
		t.Fatal(err)
	}
	if _, err := service.GetClaim(context.Background(), &mediav1.GetMediaPatchClaimRequest{SchemaVersion: 1, OperationId: "media-patch-rpc", Fence: &commonv1.ActionFence{ActionId: "media-patch-rpc", ExecutionEpoch: 2}}); err == nil {
		t.Fatal("future epoch admitted")
	}
	if _, err := service.BeginDispatch(context.Background(), nil); err == nil {
		t.Fatal("nil dispatch admitted")
	}
	if _, err := service.RecordOutcome(context.Background(), &mediav1.RecordMediaPatchOutcomeRequest{SchemaVersion: 1}); err == nil {
		t.Fatal("unbound result admitted")
	}
}
