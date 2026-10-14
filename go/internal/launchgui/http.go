package launchgui

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

type HTTPOptions struct {
	Origin, Token, StaticDir string
	OpenBrowser              func(context.Context, string) error
	OnClose                  func()
	PrepareConfig            func(launcherconfig.Config, bool) (launcherconfig.Config, error)
}

func Handler(controller *Controller, options HTTPOptions) (http.Handler, error) {
	u, err := url.Parse(options.Origin)
	if err != nil || u.Scheme != "http" || net.ParseIP(u.Hostname()) == nil || !net.ParseIP(u.Hostname()).IsLoopback() || u.Port() == "" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || len(options.Token) < 32 || strings.ContainsAny(options.Token, "\r\n\x00") {
		return nil, errors.New("invalid private launcher session")
	}
	if controller == nil || !filepath.IsAbs(options.StaticDir) {
		return nil, errors.New("launcher controller and built frontend are required")
	}
	index := filepath.Join(options.StaticDir, "index.html")
	info, err := os.Stat(index)
	if err != nil || !info.Mode().IsRegular() {
		return nil, errors.New("built launcher frontend is missing")
	}
	assetServer := http.StripPrefix("/ui/", http.FileServer(http.Dir(options.StaticDir)))
	equal := func(value string) bool { return subtle.ConstantTimeCompare([]byte(value), []byte(options.Token)) == 1 }
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; worker-src 'none'")
		if r.Host != u.Host {
			respond(w, 403, "请求地址与本地启动器不匹配")
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/launcher" && r.URL.Query().Has("token") {
			values := r.URL.Query()["token"]
			if len(values) != 1 || !equal(values[0]) {
				respond(w, 401, "启动器会话无效，请重新打开启动器")
				return
			}
			http.SetCookie(w, &http.Cookie{Name: "deepseek_launcher", Value: options.Token, Path: "/", HttpOnly: true, SameSite: http.SameSiteStrictMode})
			http.Redirect(w, r, "/launcher", http.StatusSeeOther)
			return
		}
		bearer := equal(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")) && strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ")
		cookie, err := r.Cookie("deepseek_launcher")
		if !bearer && (err != nil || !equal(cookie.Value)) {
			respond(w, 401, "启动器会话无效，请重新打开启动器")
			return
		}
		if r.Method != http.MethodGet {
			origin := r.Header.Get("Origin")
			if (origin != "" && origin != options.Origin) || (!bearer && (origin != options.Origin || r.Header.Get("X-DeepSeek-Launcher") != "1")) {
				respond(w, 403, "仅允许本地启动器中的操作")
				return
			}
			if r.Method != http.MethodPost {
				respond(w, 405, "操作方式不支持")
				return
			}
		}
		switch r.URL.Path {
		case "/", "/launcher", "/launcher/":
			if r.Method != http.MethodGet {
				respond(w, 405, "操作方式不支持")
				return
			}
			if r.URL.Path == "/" {
				http.Redirect(w, r, "/launcher", http.StatusSeeOther)
				return
			}
			http.ServeFile(w, r, index)
		case "/launcher/v1/ready":
			if r.Method != http.MethodGet {
				respond(w, 405, "操作方式不支持")
				return
			}
			encode(w, map[string]any{"ok": true, "scope": "launcher_ui"})
		case "/launcher/v1/state":
			if r.Method != http.MethodGet {
				respond(w, 405, "操作方式不支持")
				return
			}
			encode(w, controller.State())
		case "/launcher/v1/window-status":
			if r.Method != http.MethodGet {
				respond(w, 405, "操作方式不支持")
				return
			}
			encode(w, map[string]string{"status": controller.State().Status})
		case "/launcher/v1/settings":
			if r.Method != http.MethodGet {
				respond(w, 405, "操作方式不支持")
				return
			}
			value, err := controller.Settings()
			if err != nil {
				respond(w, 500, "无法读取启动器配置")
				return
			}
			encode(w, value)
		case "/launcher/v1/save", "/launcher/v1/start":
			if r.Method != http.MethodPost {
				respond(w, 405, "操作方式不支持")
				return
			}
			var value struct {
				Config            launcherconfig.Config `json:"config"`
				ConfirmMissingKey bool                  `json:"confirm_missing_key"`
				ConfirmNextPort   bool                  `json:"confirm_next_port"`
			}
			if !body(w, r, &value) {
				return
			}
			if r.URL.Path == "/launcher/v1/save" {
				if err := controller.Save(value.Config); err != nil {
					respond(w, 400, err.Error())
					return
				}
			} else {
				if value.Config.DeepSeekAPIKey == "" && !value.ConfirmMissingKey {
					respondCode(w, 409, "MISSING_KEY", ErrMissingKeyConfirmation.Error())
					return
				}
				if options.PrepareConfig != nil {
					selected, err := options.PrepareConfig(value.Config, value.ConfirmNextPort)
					if errors.Is(err, ErrPortConfirmation) {
						respondCode(w, 409, "PORT_IN_USE", err.Error())
						return
					}
					if err != nil {
						respond(w, 400, err.Error())
						return
					}
					value.Config = selected
				}
				if err := controller.Start(value.Config, value.ConfirmMissingKey); err != nil {
					respond(w, 409, err.Error())
					return
				}
			}
			encode(w, controller.State())
		case "/launcher/v1/clear", "/launcher/v1/stop", "/launcher/v1/browser", "/launcher/v1/close":
			if r.Method != http.MethodPost {
				respond(w, 405, "操作方式不支持")
				return
			}
			var value struct {
				Confirm bool `json:"confirm"`
			}
			if !body(w, r, &value) {
				return
			}
			switch r.URL.Path {
			case "/launcher/v1/clear":
				if !value.Confirm {
					respond(w, 409, "请确认清空本机保存的 API Key 与配置")
					return
				}
				if err := controller.Clear(); err != nil {
					respond(w, 500, err.Error())
					return
				}
			case "/launcher/v1/stop", "/launcher/v1/close":
				ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
				var err error
				if r.URL.Path == "/launcher/v1/close" {
					err = controller.RequestClose(ctx, value.Confirm)
				} else {
					err = controller.Stop(ctx)
				}
				cancel()
				if errors.Is(err, ErrExitConfirmation) {
					respond(w, 409, err.Error())
					return
				}
				if err != nil {
					respond(w, 503, "服务仍在停止，请等待后重试")
					return
				}
				if r.URL.Path == "/launcher/v1/close" && options.OnClose != nil {
					defer options.OnClose()
				}
			case "/launcher/v1/browser":
				state := controller.State()
				if state.Status != "running" || state.ComputerURL == "" {
					respond(w, 409, "请先启动本地服务")
					return
				}
				if options.OpenBrowser == nil {
					respond(w, 503, "无法打开浏览器，请复制地址后打开")
					return
				}
				if err := options.OpenBrowser(r.Context(), state.ComputerURL); err != nil {
					respond(w, 503, "无法打开浏览器，请复制地址后打开")
					return
				}
			}
			encode(w, controller.State())
			if r.URL.Path == "/launcher/v1/close" {
				_ = http.NewResponseController(w).Flush()
			}
		default:
			if r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/ui/assets/") {
				assetServer.ServeHTTP(w, r)
				return
			}
			http.NotFound(w, r)
		}
	}), nil
}

func body(w http.ResponseWriter, r *http.Request, value any) bool {
	media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		respond(w, 400, "需要 JSON 配置请求")
		return false
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 65536))
	trimmed := bytes.TrimSpace(raw)
	if err != nil || len(trimmed) < 2 || trimmed[0] != '{' {
		respond(w, 400, "配置请求无效")
		return false
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(value) != nil || decoder.Decode(new(any)) != io.EOF {
		respond(w, 400, "配置请求无效")
		return false
	}
	return true
}
func encode(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(value)
}
func respond(w http.ResponseWriter, status int, message string) {
	respondCode(w, status, "LAUNCHER_ERROR", message)
}
func respondCode(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message, "code": code})
}
