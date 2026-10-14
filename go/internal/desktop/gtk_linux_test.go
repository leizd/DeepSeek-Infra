//go:build linux && !android && !deepseek_android

package desktop

import (
	"errors"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ebitengine/purego"
)

func TestGTKLibraryBoundaryUsesOnlyAbsoluteSystemPaths(t *testing.T) {
	for _, architecture := range []string{"amd64", "arm64"} {
		opened, registered := 0, 0
		access := gtkLibraryAccess{
			open: func(path string, flags int) (uintptr, error) {
				if !filepath.IsAbs(path) || !strings.HasPrefix(path, "/usr/lib/") || flags != purego.RTLD_NOW|purego.RTLD_GLOBAL {
					t.Fatal("untrusted UI library search path", path)
				}
				opened++
				return uintptr(opened), nil
			},
			symbol: func(handle uintptr, name string) (uintptr, error) {
				if handle == 0 || (!strings.HasPrefix(name, "gtk_") && !strings.HasPrefix(name, "webkit_") && !strings.HasPrefix(name, "g_")) {
					t.Fatal("foreign desktop library symbol admitted", name)
				}
				return 42, nil
			},
			register: func(target any, address uintptr) { registered++ },
		}
		api, err := loadGTKFor(architecture, access)
		if err != nil || api == nil || opened != 2 || registered == 0 {
			t.Fatal("system UI contract did not load", err)
		}
	}
}

func TestGTKMissingRuntimeAndIncompatibleAPIsFailBeforeWindow(t *testing.T) {
	missing := errors.New("library unavailable")
	access := gtkLibraryAccess{open: func(string, int) (uintptr, error) {
		t.Fatal("unknown architecture reached the loader")
		return 0, missing
	}}
	if api, err := loadGTKFor("unknown", access); api != nil || err == nil {
		t.Fatal("unknown UI architecture admitted")
	}
	for _, failure := range []string{"gtk", "webkit", "symbol"} {
		access = gtkLibraryAccess{
			open: func(path string, flags int) (uintptr, error) {
				if (failure == "gtk" && strings.Contains(path, "libgtk")) || (failure == "webkit" && strings.Contains(path, "libwebkit")) {
					return 0, missing
				}
				return 1, nil
			},
			symbol:   func(uintptr, string) (uintptr, error) { return 0, missing },
			register: func(any, uintptr) { t.Fatal("missing API was registered") },
		}
		if api, err := loadGTKFor("amd64", access); api != nil || err == nil {
			t.Fatal("missing platform prerequisite admitted", failure)
		}
	}
}

type gtkTrace struct {
	api             *gtkAPI
	destroy, idle   uintptr
	unref           []uintptr
	shown, quit     bool
	cookiePath, uri string
}

func newGTKTrace(t *testing.T) *gtkTrace {
	t.Helper()
	trace := &gtkTrace{}
	trace.api = &gtkAPI{
		initCheck: func(uintptr, uintptr) int32 { return 1 },
		windowNew: func(int32) uintptr { return 1 },
		windowTitle: func(host uintptr, title string) {
			if host != 1 || title != Title {
				t.Fatal("desktop title changed")
			}
		},
		windowDefaultSize: func(host uintptr, width, height int32) {
			if width != Width || height != Height {
				t.Fatal("initial desktop dimensions changed")
			}
		},
		widgetSizeRequest: func(host uintptr, width, height int32) {
			if width != MinWidth || height != MinHeight {
				t.Fatal("minimum desktop dimensions lost")
			}
		},
		containerAdd: func(host, browser uintptr) {
			if host != 1 || browser != 2 {
				t.Fatal("wrong browser hosted")
			}
		},
		showAll: func(uintptr) { trace.shown = true },
		destroy: func(uintptr) {
			if trace.destroy != 0 {
				purego.SyscallN(trace.destroy, 1, 0)
			}
		},
		main:      func() { purego.SyscallN(trace.idle, 0) },
		mainQuit:  func() { trace.quit = true },
		mainLevel: func() uint32 { return 1 },
		signalConnect: func(host uintptr, name string, callback, data, destroy uintptr, flags uint32) uintptr {
			if name != "destroy" || host != 1 {
				t.Fatal("unexpected native signal")
			}
			trace.destroy = callback
			return 1
		},
		idleAdd: func(callback, data uintptr) uint32 {
			if trace.idle != 0 {
				t.Fatal("multiple idle callbacks allocated before dispatch")
			}
			trace.idle = callback
			return 1
		},
		unref: func(object uintptr) { trace.unref = append(trace.unref, object) },
		dataManagerNew: func(key, value, cacheKey, cacheValue string, end uintptr) uintptr {
			if key != "base-data-directory" || cacheKey != "base-cache-directory" || !filepath.IsAbs(value) || !filepath.IsAbs(cacheValue) || end != 0 {
				t.Fatal("profile escaped its configured native directories")
			}
			return 3
		},
		contextNew: func(manager uintptr) uintptr {
			if manager != 3 {
				t.Fatal("foreign website data manager")
			}
			return 4
		},
		cookieManager: func(context uintptr) uintptr {
			if context != 4 {
				t.Fatal("foreign cookie context")
			}
			return 5
		},
		cookieStorage: func(manager uintptr, path string, format int32) {
			if manager != 5 || format != 1 {
				t.Fatal("persistent cookie storage changed")
			}
			trace.cookiePath = path
		},
		webViewNew: func(uintptr) uintptr { return 2 },
		loadURI: func(browser uintptr, uri string) {
			if browser != 2 {
				t.Fatal("wrong browser navigated")
			}
			trace.uri = uri
		},
		settings: func(uintptr) uintptr { return 6 },
		developerExtras: func(settings uintptr, enabled int32) {
			if settings != 6 || enabled != 0 {
				t.Fatal("desktop developer tools enabled")
			}
		},
	}
	return trace
}

func TestGTKWindowRetainsProfileAndSerializesCancellationDispatch(t *testing.T) {
	trace := newGTKTrace(t)
	profile := t.TempDir()
	created, err := newGTKWindow(trace.api, profile)
	if err != nil {
		t.Fatal(err)
	}
	view := created.(*gtkWindow)
	view.Navigate("http://127.0.0.1:8000/?desktop=1&token=private")
	if !trace.shown || trace.cookiePath != filepath.Join(profile, "cookies.sqlite3") || trace.uri == "" || len(trace.unref) != 2 {
		t.Fatal("desktop profile or native reference lifecycle changed")
	}
	calls := 0
	view.Dispatch(func() { calls++ })
	view.Dispatch(func() { calls++; view.Destroy() })
	if err := view.Run(); err != nil || calls != 2 || !trace.quit || !view.closed {
		t.Fatal("queued cancellation did not settle on the desktop thread")
	}
	view.Destroy()
	if err := view.Run(); err != nil {
		t.Fatal("closed GTK window reentered its loop")
	}
	trace.idle = 0
	view.Dispatch(func() { t.Fatal("callback executed after window closed") })
	purego.SyscallN(trace.idle, 0)
}

func TestGTKConstructionFailuresReleaseOnlyOwnedNativeObjects(t *testing.T) {
	for _, failure := range []string{"display", "profile", "context", "window", "browser"} {
		trace := newGTKTrace(t)
		switch failure {
		case "display":
			trace.api.initCheck = func(uintptr, uintptr) int32 { return 0 }
		case "profile":
			trace.api.dataManagerNew = func(string, string, string, string, uintptr) uintptr { return 0 }
		case "context":
			trace.api.contextNew = func(uintptr) uintptr { return 0 }
		case "window":
			trace.api.windowNew = func(int32) uintptr { return 0 }
		case "browser":
			trace.api.webViewNew = func(uintptr) uintptr { return 0 }
		}
		view, err := newGTKWindow(trace.api, t.TempDir())
		if view != nil || err == nil || trace.shown {
			t.Fatal("failed native construction exposed a window", failure)
		}
		expected := map[string]int{"display": 0, "profile": 0, "context": 1, "window": 3, "browser": 2}[failure]
		if len(trace.unref) != expected {
			t.Fatal("failed construction leaked native references", failure, trace.unref)
		}
	}
}
