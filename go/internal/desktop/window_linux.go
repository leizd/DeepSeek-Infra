//go:build linux && !android && !deepseek_android

package desktop

import (
	"errors"
	"path/filepath"
	"sync"

	"github.com/ebitengine/purego"
)

type gtkWindow struct {
	api             *gtkAPI
	host, browser   uintptr
	closed          bool
	idleCallback    uintptr
	mu              sync.Mutex
	queue           []func()
	dispatchPending bool
}

func newPlatformWindow(profile string) (window, error) {
	api, err := loadGTK()
	if err != nil {
		return nil, err
	}
	return newGTKWindow(api, profile)
}

func newGTKWindow(api *gtkAPI, profile string) (window, error) {
	if api.initCheck(0, 0) == 0 {
		return nil, errors.New("cannot connect to the desktop display")
	}
	manager := api.dataManagerNew("base-data-directory", filepath.Join(profile, "data"),
		"base-cache-directory", filepath.Join(profile, "cache"), 0)
	if manager == 0 {
		return nil, errors.New("cannot create desktop profile")
	}
	defer api.unref(manager)
	context := api.contextNew(manager)
	if context == 0 {
		return nil, errors.New("cannot initialise desktop WebKit context")
	}
	defer api.unref(context)
	api.cookieStorage(api.cookieManager(context), filepath.Join(profile, "cookies.sqlite3"), 1)
	view := &gtkWindow{api: api, host: api.windowNew(0), browser: api.webViewNew(context)}
	if view.host == 0 || view.browser == 0 {
		view.Destroy()
		if view.browser != 0 {
			api.unref(view.browser)
		}
		return nil, errors.New("cannot create desktop WebKit window")
	}
	api.developerExtras(api.settings(view.browser), 0)
	api.windowTitle(view.host, Title)
	api.windowDefaultSize(view.host, Width, Height)
	api.widgetSizeRequest(view.host, MinWidth, MinHeight)
	api.containerAdd(view.host, view.browser)
	api.signalConnect(view.host, "destroy", purego.NewCallback(func(uintptr, uintptr) {
		view.closed = true
		if api.mainLevel() != 0 {
			api.mainQuit()
		}
	}), 0, 0, 0)
	// Allocate one callback per window, rather than leaking a C trampoline for
	// each cancellation or dispatch. GTK executes it on the UI thread.
	view.idleCallback = purego.NewCallback(func(uintptr) int32 {
		view.mu.Lock()
		pending := view.queue
		view.queue = nil
		view.dispatchPending = false
		view.mu.Unlock()
		if !view.closed {
			for _, call := range pending {
				call()
			}
		}
		return 0
	})
	api.showAll(view.host)
	return view, nil
}

func (view *gtkWindow) Navigate(entry string) { view.api.loadURI(view.browser, entry) }
func (view *gtkWindow) SetCloseGuard(guard func() bool) {
	view.api.signalConnect(view.host, "delete-event", purego.NewCallback(func(uintptr, uintptr, uintptr) int32 {
		if guard() {
			return 0
		}
		return 1
	}), 0, 0, 0)
}
func (view *gtkWindow) ConfirmStop() bool {
	dialog := view.api.messageDialog(view.host, 3, 1, 4, "服务仍可能在运行。退出会停止本地服务，确定退出吗？")
	if dialog == 0 {
		return false
	}
	defer view.api.destroy(dialog)
	view.api.defaultResponse(dialog, -9)
	return view.api.dialogRun(dialog) == -8
}
func (view *gtkWindow) Destroy() {
	if view.host != 0 && !view.closed {
		view.api.destroy(view.host)
	}
}
func (view *gtkWindow) Dispatch(call func()) {
	view.mu.Lock()
	view.queue = append(view.queue, call)
	if !view.dispatchPending {
		view.dispatchPending = true
		view.api.idleAdd(view.idleCallback, 0)
	}
	view.mu.Unlock()
}
func (view *gtkWindow) Run() error {
	if !view.closed {
		view.api.main()
	}
	return nil
}
