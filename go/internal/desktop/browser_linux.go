//go:build linux && !android && !deepseek_android

package desktop

import (
	"errors"
	"os"
	"runtime"
	"strings"

	"github.com/ebitengine/purego"
)

type gioAPI struct {
	contextNew func() uintptr
	unset      func(uintptr, string)
	launch     func(string, uintptr, uintptr) int32
	unref      func(uintptr)
	release    func()
}

func openPlatformBrowser(raw string) error {
	api, err := loadGIOFor(runtime.GOARCH, gtkLibraryAccess{open: purego.Dlopen, symbol: purego.Dlsym, register: purego.RegisterFunc}, purego.Dlclose)
	if err != nil {
		return err
	}
	defer api.release()
	return openGIOBrowser(raw, api, os.Environ())
}

func loadGIOFor(architecture string, access gtkLibraryAccess, close func(uintptr) error) (*gioAPI, error) {
	triplet := "x86_64-linux-gnu"
	if architecture == "arm64" {
		triplet = "aarch64-linux-gnu"
	} else if architecture != "amd64" {
		return nil, errors.New("default browser unavailable")
	}
	var library uintptr
	for _, dir := range []string{"/usr/lib/" + triplet, "/lib/" + triplet, "/usr/lib64", "/lib64", "/usr/lib", "/lib"} {
		library, _ = access.open(dir+"/libgio-2.0.so.0", purego.RTLD_NOW|purego.RTLD_LOCAL)
		if library != 0 {
			break
		}
	}
	if library == 0 {
		return nil, errors.New("default browser unavailable")
	}
	api := &gioAPI{release: func() { _ = close(library) }}
	for _, binding := range []struct {
		name   string
		target any
	}{{"g_app_launch_context_new", &api.contextNew}, {"g_app_launch_context_unsetenv", &api.unset}, {"g_app_info_launch_default_for_uri", &api.launch}, {"g_object_unref", &api.unref}} {
		symbol, err := access.symbol(library, binding.name)
		if err != nil {
			api.release()
			return nil, errors.New("default browser API unavailable")
		}
		access.register(binding.target, symbol)
	}
	return api, nil
}

func openGIOBrowser(raw string, api *gioAPI, env []string) error {
	context := api.contextNew()
	if context == 0 {
		return errors.New("default browser context unavailable")
	}
	defer api.unref(context)
	// Launch context environment applies only to the external UI, not to this
	// supervisor or the already-admitted backend processes.
	for _, value := range env {
		key, _, _ := strings.Cut(value, "=")
		if !browserSessionKey(key) {
			api.unset(context, key)
		}
	}
	if api.launch(raw, context, 0) == 0 {
		return errors.New("default browser unavailable")
	}
	return nil
}

func browserSessionKey(key string) bool {
	for _, allowed := range []string{"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "XDG_CURRENT_DESKTOP", "DESKTOP_SESSION"} {
		if key == allowed {
			return true
		}
	}
	return false
}
