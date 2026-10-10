//go:build linux && !android && !deepseek_android

package desktop

import (
	"errors"
	"fmt"
	"runtime"

	"github.com/ebitengine/purego"
)

// GTK/WebKit are platform UI APIs only. No business bridge or Rust/Go FFI is
// installed. Signatures follow GTK 3 and WebKitGTK 4.1's public ABI.
type gtkAPI struct {
	initCheck         func(uintptr, uintptr) int32
	windowNew         func(int32) uintptr
	windowTitle       func(uintptr, string)
	windowDefaultSize func(uintptr, int32, int32)
	widgetSizeRequest func(uintptr, int32, int32)
	containerAdd      func(uintptr, uintptr)
	showAll           func(uintptr)
	destroy           func(uintptr)
	main              func()
	mainQuit          func()
	mainLevel         func() uint32
	signalConnect     func(uintptr, string, uintptr, uintptr, uintptr, uint32) uintptr
	idleAdd           func(uintptr, uintptr) uint32
	unref             func(uintptr)
	dataManagerNew    func(string, string, string, string, uintptr) uintptr
	contextNew        func(uintptr) uintptr
	cookieManager     func(uintptr) uintptr
	cookieStorage     func(uintptr, string, int32)
	webViewNew        func(uintptr) uintptr
	loadURI           func(uintptr, string)
	settings          func(uintptr) uintptr
	developerExtras   func(uintptr, int32)
	messageDialog     func(uintptr, int32, int32, int32, string) uintptr
	dialogRun         func(uintptr) int32
	defaultResponse   func(uintptr, int32)
}

type gtkLibraryAccess struct {
	open     func(string, int) (uintptr, error)
	symbol   func(uintptr, string) (uintptr, error)
	register func(any, uintptr)
}

func loadGTK() (*gtkAPI, error) {
	return loadGTKFor(runtime.GOARCH, gtkLibraryAccess{open: purego.Dlopen, symbol: purego.Dlsym, register: purego.RegisterFunc})
}

func loadGTKFor(architecture string, access gtkLibraryAccess) (*gtkAPI, error) {
	var triplet string
	switch architecture {
	case "amd64":
		triplet = "x86_64-linux-gnu"
	case "arm64":
		triplet = "aarch64-linux-gnu"
	default:
		return nil, errors.New("GTK desktop requires a supported system WebKitGTK runtime")
	}
	// Absolute system paths exclude libraries in the workspace or current directory.
	openSystemLibrary := func(name string) (uintptr, error) {
		for _, dir := range []string{"/usr/lib/" + triplet, "/lib/" + triplet, "/usr/lib64", "/lib64", "/usr/lib", "/lib"} {
			if handle, err := access.open(dir+"/"+name, purego.RTLD_NOW|purego.RTLD_GLOBAL); err == nil {
				return handle, nil
			}
		}
		return 0, errors.New("missing system UI library")
	}
	gtk, err := openSystemLibrary("libgtk-3.so.0")
	if err != nil {
		return nil, errors.New("GTK 3 is required for the desktop window")
	}
	webkit, err := openSystemLibrary("libwebkit2gtk-4.1.so.0")
	if err != nil {
		return nil, errors.New("WebKitGTK 4.1 is required for the desktop window")
	}
	api := &gtkAPI{}
	bindings := []struct {
		handle uintptr
		name   string
		target any
	}{
		{gtk, "gtk_init_check", &api.initCheck},
		{gtk, "gtk_window_new", &api.windowNew},
		{gtk, "gtk_window_set_title", &api.windowTitle},
		{gtk, "gtk_window_set_default_size", &api.windowDefaultSize},
		{gtk, "gtk_widget_set_size_request", &api.widgetSizeRequest},
		{gtk, "gtk_container_add", &api.containerAdd},
		{gtk, "gtk_widget_show_all", &api.showAll},
		{gtk, "gtk_widget_destroy", &api.destroy},
		{gtk, "gtk_main", &api.main},
		{gtk, "gtk_main_quit", &api.mainQuit},
		{gtk, "gtk_main_level", &api.mainLevel},
		{gtk, "g_signal_connect_data", &api.signalConnect},
		{gtk, "g_idle_add", &api.idleAdd},
		{gtk, "g_object_unref", &api.unref},
		{gtk, "gtk_message_dialog_new", &api.messageDialog},
		{gtk, "gtk_dialog_run", &api.dialogRun},
		{gtk, "gtk_dialog_set_default_response", &api.defaultResponse},
		{webkit, "webkit_website_data_manager_new", &api.dataManagerNew},
		{webkit, "webkit_web_context_new_with_website_data_manager", &api.contextNew},
		{webkit, "webkit_web_context_get_cookie_manager", &api.cookieManager},
		{webkit, "webkit_cookie_manager_set_persistent_storage", &api.cookieStorage},
		{webkit, "webkit_web_view_new_with_context", &api.webViewNew},
		{webkit, "webkit_web_view_load_uri", &api.loadURI},
		{webkit, "webkit_web_view_get_settings", &api.settings},
		{webkit, "webkit_settings_set_enable_developer_extras", &api.developerExtras},
	}
	for _, binding := range bindings {
		symbol, err := access.symbol(binding.handle, binding.name)
		if err != nil {
			return nil, fmt.Errorf("missing desktop system API %s", binding.name)
		}
		access.register(binding.target, symbol)
	}
	return api, nil
}
