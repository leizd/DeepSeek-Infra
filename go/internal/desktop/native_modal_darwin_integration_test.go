//go:build darwin && !ios && native_desktop_integration

package desktop

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime"
	"testing"
	"time"

	"github.com/ebitengine/purego/objc"
)

// The observer runs through public AppKit modal modes. The application is
// cancelled only through its original context/Dispatch/Destroy path.
// https://developer.apple.com/documentation/appkit/nsapplication/abortmodal()
func TestNativeMacJavaScriptModalCancellationSettlesApplication(t *testing.T) {
	if os.Getenv("GITHUB_ACTIONS") != "true" || os.Getenv("GITHUB_EVENT_NAME") != "workflow_dispatch" || os.Getenv("DEEPSEEK_MACOS_ARCH") != runtime.GOARCH {
		t.Fatal("native modal verification requires the actual architecture on a disposable cloud Mac")
	}
	profile, reportPath := os.Getenv("DEEPSEEK_MACOS_PROFILE"), os.Getenv("DEEPSEEK_MACOS_MODAL_REPORT")
	if profile == "" || reportPath == "" {
		t.Fatal("native modal verification profile and report are required")
	}
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	bytes, err := os.ReadFile(executable)
	if err != nil {
		t.Fatal(err)
	}
	report := map[string]any{"status": "FAIL", "sourceCommit": os.Getenv("GITHUB_SHA"), "runId": os.Getenv("GITHUB_RUN_ID"),
		"attempt": os.Getenv("GITHUB_RUN_ATTEMPT"), "architecture": runtime.GOARCH, "pid": os.Getpid(),
		"actualExecutableSha256": fmt.Sprintf("%x", sha256.Sum256(bytes)),
		"scope":                  "real WebKit JavaScript alert and original native context cancellation only", "testAbortUsed": false,
		"contextCancellationSettled": false, "allDesktopFeaturesQualified": false, "fullMigrationQualified": false, "readiness": "NOT_READY"}
	defer func() {
		data, marshalErr := json.MarshalIndent(report, "", "  ")
		if marshalErr != nil || os.WriteFile(reportPath, append(data, '\n'), 0o600) != nil {
			t.Error("cannot retain the actual modal cancellation observation")
		}
	}()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(`<!doctype html><html><body><h1>Native modal cancellation fixture</h1><script>
alert('DeepSeek actual native modal cancellation fixture');
</script></body></html>`))
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	type modalObservation struct {
		windowID int64
		main     bool
	}
	type observer struct {
		target, modes objc.ID
		view          *macWindow
	}
	type completion struct {
		err                          error
		actions, responses, dispatch uint64
	}
	created, modal, finished := make(chan observer, 1), make(chan modalObservation, 1), make(chan completion, 1)
	macUITasks <- func() {
		var actual *macWindow
		var object, modes objc.ID
		runErr := show(ctx, server.URL, profile, func(path string) (window, error) {
			view, createErr := newPlatformWindow(path)
			if createErr != nil {
				return nil, createErr
			}
			actual = view.(*macWindow)
			modes = macClass("NSMutableArray").Send(macSEL("new"))
			for _, name := range []string{"NSDefaultRunLoopMode", "NSModalPanelRunLoopMode", "NSEventTrackingRunLoopMode"} {
				modes.Send(macSEL("addObject:"), macString(name))
			}
			observe := func(_ objc.ID, _ objc.SEL, _ objc.ID) {
				panel := actual.app.Send(macSEL("modalWindow"))
				if panel == 0 || !objc.Send[bool](panel, macSEL("isVisible")) {
					return
				}
				seen := modalObservation{objc.Send[int64](panel, macSEL("windowNumber")), objc.Send[bool](macClass("NSThread"), macSEL("isMainThread"))}
				select {
				case modal <- seen:
				default:
				}
			}
			class, classErr := objc.RegisterClass(fmt.Sprintf("DeepSeekModalObserver%d", os.Getpid()), objc.GetClass("NSObject"), nil, nil,
				[]objc.MethodDef{{Cmd: macSEL("observeNativeModal:"), Fn: observe}})
			if classErr != nil {
				view.Destroy()
				return nil, classErr
			}
			object = objc.ID(class).Send(macSEL("new"))
			if object == 0 {
				view.Destroy()
				return nil, errors.New("native modal observer unavailable")
			}
			created <- observer{object, modes, actual}
			return view, nil
		})
		var result completion
		result.err = runErr
		if actual != nil {
			result.actions, result.responses, result.dispatch = actual.navigationActions, actual.navigationResponses, actual.queueDrains
		}
		if object != 0 {
			object.Send(macSEL("release"))
		}
		if modes != 0 {
			modes.Send(macSEL("release"))
		}
		finished <- result
	}
	var observed observer
	select {
	case observed = <-created:
	case result := <-finished:
		t.Fatal("native modal host ended before creating its actual window", result.err)
	case <-ctx.Done():
		t.Fatal("native modal host was not created")
	}
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			// This is observation, not a substitute application cancellation.
			observed.target.Send(macSEL("performSelectorOnMainThread:withObject:waitUntilDone:modes:"),
				macSEL("observeNativeModal:"), objc.ID(0), false, observed.modes)
		case seen := <-modal:
			if seen.windowID <= 0 || !seen.main {
				t.Fatal("actual native modal was not observed on the initial thread")
			}
			report["actualModalWindowId"], report["nativeMainThreadObserved"] = seen.windowID, seen.main
			report["actualJavaScriptModalObserved"] = true
			ticker.Stop()
			cancel()
			select {
			case result := <-finished:
				if result.err != nil || result.actions == 0 || result.responses == 0 || result.dispatch == 0 {
					t.Fatal("native modal cancellation or original callback path failed", result.err)
				}
				report["status"], report["contextCancellationSettled"] = "PASS", true
				report["nativeNavigationActionCallbacks"], report["nativeNavigationResponseCallbacks"], report["nativeDispatchCallbacks"] = result.actions, result.responses, result.dispatch
				return
			case <-time.After(5 * time.Second):
				observed.view.mu.Lock()
				report["destroyReached"] = observed.view.closed
				observed.view.mu.Unlock()
				report["failure"] = "NATIVE_MODAL_CONTEXT_CANCELLATION_DID_NOT_SETTLE"
				t.Fatal("original native context cancellation did not leave the modal and application loops")
			}
		case result := <-finished:
			t.Fatal("native host ended before the actual JavaScript modal was observed", result.err)
		case <-ctx.Done():
			report["failure"] = "NATIVE_JAVASCRIPT_MODAL_NOT_OBSERVED"
			t.Fatal("actual WebKit JavaScript modal not observed")
		}
	}
}
