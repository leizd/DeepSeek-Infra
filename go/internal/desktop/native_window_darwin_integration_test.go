//go:build darwin && !ios && native_desktop_integration

package desktop

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime"
	"strconv"
	"structs"
	"testing"
	"time"

	"github.com/ebitengine/purego/objc"
)

// AppKit requires the initial thread, including in the native Go test host.
// Source: https://github.com/ebitengine/purego/blob/v0.11.1/examples/window/main_darwin.go
func init() { runtime.LockOSThread() }

var macUITasks = make(chan func())

func TestMain(m *testing.M) {
	done := make(chan int, 1)
	go func() { done <- m.Run() }()
	for {
		select {
		case task := <-macUITasks:
			task()
		case code := <-done:
			os.Exit(code)
		}
	}
}

type macTestPoint struct {
	_    structs.HostLayout
	X, Y float64
}

type macTestSize struct {
	_             structs.HostLayout
	Width, Height float64
}

type macTestRect struct {
	_      structs.HostLayout
	Origin macTestPoint
	Size   macTestSize
}

func TestNativeMacWindowBootAndCancel(t *testing.T) {
	if os.Getenv("GITHUB_ACTIONS") != "true" || os.Getenv("RUNNER_ENVIRONMENT") != "github-hosted" ||
		os.Getenv("RUNNER_OS") != "macOS" || os.Getenv("GITHUB_REPOSITORY") != "leizd/DeepSeek-Infra" ||
		os.Getenv("GITHUB_EVENT_NAME") != "workflow_dispatch" || runtime.GOARCH != os.Getenv("DEEPSEEK_MACOS_ARCH") {
		t.Fatal("explicit matching cloud macOS runtime is required")
	}
	profile, reportPath := os.Getenv("DEEPSEEK_MACOS_PROFILE"), os.Getenv("DEEPSEEK_MACOS_REPORT")
	if profile == "" || reportPath == "" {
		t.Fatal("disposable macOS profile and report paths are required")
	}
	type browserObservation struct{ width, height int }
	loaded := make(chan browserObservation, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("token") == "native-macos-fixture-only" {
			http.SetCookie(w, &http.Cookie{Name: "native_mac_fixture", Value: "native-cookie", Path: "/", HttpOnly: true, SameSite: http.SameSiteStrictMode})
			http.Redirect(w, r, "/?desktop=1", http.StatusFound)
			return
		}
		cookie, err := r.Cookie("native_mac_fixture")
		if err != nil || cookie.Value != "native-cookie" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		if r.URL.Path == "/loaded" {
			width, _ := strconv.Atoi(r.URL.Query().Get("width"))
			height, _ := strconv.Atoi(r.URL.Query().Get("height"))
			if r.URL.Query().Get("text") == "true" {
				select {
				case loaded <- browserObservation{width, height}:
				default:
				}
			}
			w.WriteHeader(http.StatusNoContent)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(`<!doctype html><html><body><h1>DeepSeek native macOS fixture</h1><script>
fetch('/loaded?width='+innerWidth+'&height='+innerHeight+'&text='+document.body.textContent.includes('DeepSeek native macOS fixture'));
</script></body></html>`))
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	type completion struct {
		err     error
		receipt map[string]any
	}
	finished := make(chan completion, 1)
	macUITasks <- func() {
		receipt := map[string]any{"scope": "controlled native Go macOS window, authenticated fixture DOM and cancellation only",
			"sourceCommit": os.Getenv("GITHUB_SHA"), "runId": os.Getenv("GITHUB_RUN_ID"), "attempt": os.Getenv("GITHUB_RUN_ATTEMPT"),
			"architecture": runtime.GOARCH, "pid": os.Getpid(), "productionApplicationWorkloadQualified": false,
			"persistentProfileQualified": false, "allDesktopFeaturesQualified": false, "fullMigrationQualified": false, "readiness": "NOT_READY"}
		err := show(ctx, server.URL+"/?desktop=1&token=native-macos-fixture-only", profile, func(path string) (window, error) {
			view, err := newPlatformWindow(path)
			if err != nil {
				return nil, err
			}
			app := objc.ID(objc.GetClass("NSApplication")).Send(objc.RegisterName("sharedApplication"))
			windows := app.Send(objc.RegisterName("windows"))
			title := objc.ID(objc.GetClass("NSString")).Send(objc.RegisterName("stringWithUTF8String:"), Title)
			count := objc.Send[uint64](windows, objc.RegisterName("count"))
			for index := uint64(0); index < count && index < 16; index++ {
				candidate := windows.Send(objc.RegisterName("objectAtIndex:"), index)
				actualTitle := candidate.Send(objc.RegisterName("title"))
				if !objc.Send[bool](candidate, objc.RegisterName("isVisible")) || !objc.Send[bool](actualTitle, objc.RegisterName("isEqualToString:"), title) {
					continue
				}
				content := candidate.Send(objc.RegisterName("contentView"))
				frame := objc.Send[macTestRect](content, objc.RegisterName("frame"))
				receipt["windowId"] = objc.Send[int64](candidate, objc.RegisterName("windowNumber"))
				receipt["visible"] = true
				receipt["titleMatched"] = true
				receipt["contentWidth"], receipt["contentHeight"] = frame.Size.Width, frame.Size.Height
				return view, nil
			}
			view.Destroy()
			return nil, errors.New("real visible macOS application window not observed")
		})
		finished <- completion{err, receipt}
	}
	var browser browserObservation
	select {
	case result := <-finished:
		t.Fatal("native macOS host ended before its authenticated DOM workload", result.err)
	case browser = <-loaded:
		if browser.width < MinWidth || browser.height < MinHeight {
			t.Fatal("native WebKit document layout differs")
		}
	case <-ctx.Done():
		t.Fatal("native WebKit DOM and authenticated cookie exchange were not observed")
	}
	cancel()
	select {
	case result := <-finished:
		if result.err != nil {
			t.Fatal("native macOS host did not settle cancellation", result.err)
		}
		result.receipt["authenticatedDOMObserved"], result.receipt["contextCancellationSettled"] = true, true
		result.receipt["documentWidth"], result.receipt["documentHeight"] = browser.width, browser.height
		data, err := json.MarshalIndent(result.receipt, "", "  ")
		if err != nil || os.WriteFile(reportPath, append(data, '\n'), 0o600) != nil {
			t.Fatal("cannot retain native macOS observation")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("native macOS event loop did not stop after cancellation")
	}
}
