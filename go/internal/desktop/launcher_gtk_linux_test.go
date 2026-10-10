//go:build linux && !android && !deepseek_android

package desktop

import (
	"testing"

	"github.com/ebitengine/purego"
)

func TestGTKLauncherConfirmationDefaultsToNoAndReleasesItsDialog(t *testing.T) {
	for _, response := range []int32{-8, -9, -4} {
		destroyed, runs := 0, 0
		api := &gtkAPI{
			messageDialog: func(parent uintptr, flags, kind, buttons int32, message string) uintptr {
				if parent != 42 || flags != 3 || kind != 1 || buttons != 4 || message == "" {
					t.Fatal("launcher warning lost its host or safe button contract")
				}
				return 77
			},
			defaultResponse: func(dialog uintptr, selected int32) {
				if dialog != 77 || selected != -9 {
					t.Fatal("confirmation default was not No")
				}
			},
			dialogRun: func(dialog uintptr) int32 {
				if dialog != 77 {
					t.Fatal("foreign dialog ran")
				}
				runs++
				return response
			},
			destroy: func(dialog uintptr) {
				if dialog != 77 {
					t.Fatal("foreign widget destroyed")
				}
				destroyed++
			},
		}
		view := &gtkWindow{api: api, host: 42}
		if got := view.ConfirmStop(); got != (response == -8) || runs != 1 || destroyed != 1 {
			t.Fatal("No, window-close, or dialog ownership changed", response, got, runs, destroyed)
		}
	}
	view := &gtkWindow{api: &gtkAPI{messageDialog: func(uintptr, int32, int32, int32, string) uintptr { return 0 }}, host: 42}
	if view.ConfirmStop() {
		t.Fatal("failed warning dialog permitted service shutdown")
	}
}

func TestGTKLauncherDeleteSignalHonoursTheActualCloseGuard(t *testing.T) {
	var callback uintptr
	view := &gtkWindow{host: 42, api: &gtkAPI{signalConnect: func(host uintptr, name string, handler, data, destroy uintptr, flags uint32) uintptr {
		if host != 42 || name != "delete-event" || handler == 0 || data != 0 || destroy != 0 || flags != 0 {
			t.Fatal("invalid launcher close signal contract")
		}
		callback = handler
		return 1
	}}}
	allow := false
	view.SetCloseGuard(func() bool { return allow })
	if result, _, _ := purego.SyscallN(callback, 42, 0, 0); result != 1 {
		t.Fatal("cancelled close was not vetoed")
	}
	allow = true
	if result, _, _ := purego.SyscallN(callback, 42, 0, 0); result != 0 {
		t.Fatal("confirmed close was vetoed")
	}
}
