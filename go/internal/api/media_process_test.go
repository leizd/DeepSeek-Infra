package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestMediaProcessOptionsPreserveEmptyBodyTruthTokensAndCorrelation(t *testing.T) {
	for _, test := range []struct {
		name, body, query string
		force             bool
		ocr               *bool
	}{
		{name: "empty"}, {name: "empty-object", body: `{}`},
		{name: "query", query: "?force=off&force=ON", force: true},
		{name: "bool", body: `{"force":true,"ocrEnabled":false}`, force: true, ocr: boolPointer(false)},
		{name: "integer", body: `{"force":1,"ocrEnabled":"YES"}`, force: true, ocr: boolPointer(true)},
		{name: "float-is-not-token", body: `{"force":1.0,"ocrEnabled":1e0}`, ocr: boolPointer(false)},
		{name: "unicode-trim", body: `{"force":"\u3000On\u3000"}`, force: true},
		{name: "containers-are-not-tokens", body: `{"force":[1],"ocrEnabled":{"x":1}}`, ocr: boolPointer(false)},
		{name: "ignored-credential-without-ocr", body: `{"apiKey":"ignored fixture value","ocrEnabled":false}`, ocr: boolPointer(false)},
	} {
		t.Run(test.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodPost, "http://127.0.0.1/api/media/media_one/process"+test.query, strings.NewReader(test.body))
			r.Header.Set("X-DeepSeek-Request-ID", " original.request:1 ")
			w := httptest.NewRecorder()
			p, ok := parseMediaProcessOptions(w, r)
			if !ok || p.Force != test.force || p.CorrelationId != "original.request:1" || (p.OcrEnabled == nil) != (test.ocr == nil) || (p.OcrEnabled != nil && *p.OcrEnabled != *test.ocr) {
				t.Fatalf("options changed: %+v want %+v body=%s", p, test, w.Body.String())
			}
		})
	}
	for _, header := range []string{"", "unsafe header \"", strings.Repeat("a", 65), "中文"} {
		r := httptest.NewRequest(http.MethodPost, "http://127.0.0.1/api/media/media_one/process", nil)
		r.Header.Set("X-DeepSeek-Request-ID", header)
		p, ok := parseMediaProcessOptions(httptest.NewRecorder(), r)
		if !ok || len(p.CorrelationId) != 32 || p.CorrelationId == header {
			t.Fatal("unsafe or absent correlation was echoed", p)
		}
	}
}

func boolPointer(value bool) *bool { return &value }

func TestMediaPublicProcessAdmissionRejectsUnboundRequestsBeforeCreatingOperations(t *testing.T) {
	for _, method := range []string{http.MethodGet, http.MethodHead, http.MethodDelete, http.MethodPut} {
		r := httptest.NewRequest(method, "http://127.0.0.1/api/media/media_one/process", nil)
		w := httptest.NewRecorder()
		mediaProcess(w, r, nil, PublicView{})
		if w.Code != 405 || w.Header().Get("Allow") != "POST" {
			t.Fatal("process method semantics changed", method, w.Code, w.Header())
		}
	}
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "process-admission"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	t.Setenv("DEEPSEEK_INFRA_ROOT", t.TempDir())
	t.Setenv("AUTH_DISABLED", "0")
	t.Setenv("AUTH_TOKEN", strings.Repeat("c", 32))
	t.Setenv("DEEPSEEK_WORKER_SERVICE_BEARER", "")
	for _, test := range []struct {
		name, body, host, mode, runtime string
		token                           bool
		status                          int
		code                            string
	}{
		{name: "auth", status: 401, code: "unauthorized"},
		{name: "host", host: "untrusted.invalid", token: true, status: 403, code: "forbidden"},
		{name: "shadow", token: true, mode: config.ModeShadow, status: 503, code: "MEDIA_AUTHORITY_UNAVAILABLE"},
		{name: "python", token: true, runtime: "python", status: 503, code: "MEDIA_AUTHORITY_UNAVAILABLE"},
		{name: "malformed", token: true, body: `{`, status: 400, code: "invalid_payload"},
		{name: "array", token: true, body: `[]`, status: 400, code: "invalid_payload"},
		{name: "trailing", token: true, body: `{} {}`, status: 400, code: "invalid_payload"},
		{name: "declared-body-limit", token: true, body: `{}`, status: 413, code: "upload_too_large"},
		{name: "nested-path", token: true, status: 501, code: "GO_API_NOT_IMPLEMENTED"},
		{name: "credential-not-silently-ignored", token: true, body: `{"ocrEnabled":true,"apiKey":"fixture caller override"}`, status: 501, code: "MEDIA_OCR_CREDENTIAL_OVERRIDE_NOT_READY"},
		{name: "legal-empty-requires-worker", token: true, status: 503, code: "MEDIA_WORKER_UNAVAILABLE"},
		{name: "legal-force-requires-worker", token: true, body: `{"force":true}`, status: 503, code: "MEDIA_WORKER_UNAVAILABLE"},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
			if test.runtime != "" {
				t.Setenv("DEEPSEEK_RUNTIME_MODE", test.runtime)
			}
			view := PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo}
			if test.mode != "" {
				view.Mode = test.mode
			}
			mux := http.NewServeMux()
			RegisterPublicView(mux, control, view)
			r := httptest.NewRequest(http.MethodPost, "http://127.0.0.1/api/media/media_one/process", strings.NewReader(test.body))
			if test.name == "declared-body-limit" {
				r.ContentLength = 2_000_001
			}
			if test.name == "nested-path" {
				r.URL.Path = "/api/media/a/b/process"
			}
			if test.token {
				r.Header.Set("Authorization", "Bearer "+strings.Repeat("c", 32))
			}
			if test.host != "" {
				r.Host = test.host
			}
			w := httptest.NewRecorder()
			mux.ServeHTTP(w, r)
			var body map[string]any
			if err = json.Unmarshal(w.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			actualCode := body["code"]
			if test.name == "nested-path" {
				// Preserve the existing notImplemented envelope on unmatched paths.
				nested, ok := body["error"].(map[string]any)
				if !ok || nested["message"] != "this public /api path is not served by the Go control plane yet" {
					t.Fatal("unmatched-path error envelope changed", body)
				}
				actualCode = nested["code"]
			}
			if w.Code != test.status || actualCode != test.code {
				t.Fatalf("%d %s want %d/%s", w.Code, w.Body.String(), test.status, test.code)
			}
			pending, e := control.PendingMediaPatch()
			if e != nil || pending != nil {
				t.Fatal("pre-admission failure invented an operation", e)
			}
		})
	}
}
