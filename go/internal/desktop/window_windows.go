//go:build windows

package desktop

import (
	"errors"
	"runtime"
	"sync"
	"syscall"
	"unsafe"

	"github.com/wailsapp/go-webview2/pkg/edge"
	"github.com/wailsapp/go-webview2/webviewloader"
	"golang.org/x/sys/windows"
)

var user32 = windows.NewLazySystemDLL("user32.dll")
var createWindow = user32.NewProc("CreateWindowExW")
var destroyWindow = user32.NewProc("DestroyWindow")
var defaultWindowProc = user32.NewProc("DefWindowProcW")
var postMessage = user32.NewProc("PostMessageW")
var nativeWindows sync.Map

type nativePoint struct{ X, Y int32 }
type nativeMinMax struct{ Reserved, MaxSize, MaxPosition, MinTrack, MaxTrack nativePoint }
type nativeClass struct {
	Size, Style                        uint32
	Callback                           uintptr
	ClassExtra, WindowExtra            int32
	Instance, Icon, Cursor, Background uintptr
	Menu, Name                         *uint16
	SmallIcon                          uintptr
}
type nativeMessage struct {
	Window         uintptr
	Message        uint32
	WParam, LParam uintptr
	Time           uint32
	Point          nativePoint
	Private        uint32
}

type nativeWindow struct {
	hwnd       uintptr
	browser    *edge.Chromium
	ready      bool
	closed     bool
	comOwned   bool
	mu         sync.Mutex
	queue      []func()
	closeGuard func() bool
}

// The host owns only the Win32 window/message loop. The pinned SDK binding
// supplies Chromium; browser permissions keep the SDK default prompt policy.
func newPlatformWindow(profile string) (window, error) {
	if err := windows.SetDefaultDllDirectories(windows.LOAD_LIBRARY_SEARCH_SYSTEM32); err != nil {
		return nil, errors.New("cannot secure desktop DLL loading")
	}
	version, err := webviewloader.GetAvailableCoreWebView2BrowserVersionString("")
	if err != nil || version == "" {
		return nil, errors.New("Microsoft WebView2 Runtime is required for the desktop window")
	}
	if err := windows.CoInitializeEx(0, windows.COINIT_APARTMENTTHREADED); err != nil && err != syscall.Errno(1) {
		return nil, errors.New("cannot initialise desktop COM apartment")
	}
	comTransferred := false
	defer func() {
		if !comTransferred {
			windows.CoUninitialize()
		}
	}()
	name, _ := windows.UTF16PtrFromString("DeepSeekNativeDesktopV1")
	var instance windows.Handle
	if err := windows.GetModuleHandleEx(0, nil, &instance); err != nil {
		return nil, errors.New("cannot initialise desktop window class")
	}
	class := nativeClass{Callback: windows.NewCallback(desktopWindowProc), Instance: uintptr(instance), Name: name}
	class.Size = uint32(unsafe.Sizeof(class))
	if result, _, last := user32.NewProc("RegisterClassExW").Call(uintptr(unsafe.Pointer(&class))); result == 0 && last != windows.ERROR_CLASS_ALREADY_EXISTS {
		return nil, errors.New("cannot register desktop window class")
	}
	title, _ := windows.UTF16PtrFromString(Title)
	// WS_OVERLAPPEDWINDOW; CW_USEDEFAULT placement preserves normal OS behaviour.
	hwnd, _, _ := createWindow.Call(0, uintptr(unsafe.Pointer(name)), uintptr(unsafe.Pointer(title)), 0x00cf0000, 0x80000000, 0x80000000, Width, Height, 0, 0, uintptr(instance), 0)
	if hwnd == 0 {
		return nil, errors.New("cannot create the desktop window")
	}
	view := &nativeWindow{hwnd: hwnd, browser: edge.NewChromium()}
	nativeWindows.Store(hwnd, view)
	view.browser.DataPath = profile
	view.browser.Debug = false
	view.browser.SetGlobalPermission(edge.CoreWebView2PermissionStateDefault)
	if !view.browser.Embed(hwnd) {
		view.close()
		return nil, errors.New("cannot initialise desktop WebView2")
	}
	settings, err := view.browser.GetSettings()
	if err != nil {
		view.close()
		return nil, errors.New("cannot configure desktop WebView2")
	}
	if err = settings.PutAreDevToolsEnabled(false); err != nil {
		view.close()
		return nil, errors.New("cannot configure desktop developer tools")
	}
	if err = settings.PutIsWebMessageEnabled(false); err != nil {
		view.close()
		return nil, errors.New("cannot isolate desktop host messages")
	}
	view.ready = true
	view.browser.Resize()
	_, _, _ = user32.NewProc("ShowWindow").Call(hwnd, 5)
	_, _, _ = user32.NewProc("UpdateWindow").Call(hwnd)
	view.comOwned = true
	comTransferred = true
	return view, nil
}

func desktopWindowProc(hwnd, message, wparam uintptr, lparam unsafe.Pointer) uintptr {
	if value, ok := nativeWindows.Load(hwnd); ok {
		view := value.(*nativeWindow)
		switch message {
		case 0x0010:
			if view.closeGuard != nil && !view.closeGuard() {
				return 0
			}
			view.close()
			return 0
		case 0x0002:
			_, _, _ = user32.NewProc("PostQuitMessage").Call(0)
			return 0
		case 0x0005:
			if view.ready && !view.closed {
				view.browser.Resize()
			}
			return 0
		case 0x0007: // WM_SETFOCUS
			if view.ready && !view.closed {
				_ = view.browser.GetController().MoveFocus(edge.COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC)
			}
			return 0
		case 0x0024:
			bounds := (*nativeMinMax)(lparam)
			bounds.MinTrack = nativePoint{MinWidth, MinHeight}
			return 0
		case 0x8001:
			view.mu.Lock()
			pending := view.queue
			view.queue = nil
			view.mu.Unlock()
			for _, callback := range pending {
				callback()
			}
			return 0
		case 0x8002: // Owned cancellation and cleanup bypass the user-close prompt.
			view.close()
			return 0
		}
	}
	result, _, _ := defaultWindowProc.Call(hwnd, message, wparam, uintptr(lparam))
	return result
}

func (view *nativeWindow) Navigate(entry string)           { view.browser.Navigate(entry) }
func (view *nativeWindow) Destroy()                        { _, _, _ = postMessage.Call(view.hwnd, 0x8002, 0, 0) }
func (view *nativeWindow) SetCloseGuard(guard func() bool) { view.closeGuard = guard }
func (view *nativeWindow) ConfirmStop() bool {
	message, _ := windows.UTF16PtrFromString("服务仍可能在运行。退出会停止本地服务，确定退出吗？")
	caption, _ := windows.UTF16PtrFromString("DeepSeek Infra 启动器")
	// Yes/No, warning, with No selected by default.
	result, _, _ := user32.NewProc("MessageBoxW").Call(view.hwnd, uintptr(unsafe.Pointer(message)), uintptr(unsafe.Pointer(caption)), 0x134)
	return result == 6
}
func (view *nativeWindow) Dispatch(callback func()) {
	view.mu.Lock()
	view.queue = append(view.queue, callback)
	view.mu.Unlock()
	_, _, _ = postMessage.Call(view.hwnd, 0x8001, 0, 0)
}
func (view *nativeWindow) Run() error {
	defer nativeWindows.Delete(view.hwnd)
	var message nativeMessage
	for {
		result, _, _ := user32.NewProc("GetMessageW").Call(uintptr(unsafe.Pointer(&message)), 0, 0, 0)
		if int32(result) == -1 {
			return errors.New("desktop message loop failed")
		}
		if result == 0 {
			return nil
		}
		_, _, _ = user32.NewProc("TranslateMessage").Call(uintptr(unsafe.Pointer(&message)))
		_, _, _ = user32.NewProc("DispatchMessageW").Call(uintptr(unsafe.Pointer(&message)))
	}
}
func (view *nativeWindow) close() {
	if view.closed {
		return
	}
	view.closed = true
	view.browser.ShuttingDown()
	if controller := view.browser.GetController(); controller != nil {
		// ICoreWebView2Controller's frozen SDK vtable has 24 entries preceding
		// Close (IUnknown plus the 21 controller methods). Avoid importing the
		// separate generated callback package, whose by-value GUID callbacks
		// cannot be registered with the pinned Go syscall.NewCallback.
		type controllerCloseTable struct {
			preceding [24]uintptr
			close     uintptr
		}
		table := *(**controllerCloseTable)(unsafe.Pointer(controller))
		_, _, _ = syscall.SyscallN(table.close, uintptr(unsafe.Pointer(controller)))
		runtime.KeepAlive(controller)
	}
	_, _, _ = destroyWindow.Call(view.hwnd)
	nativeWindows.Delete(view.hwnd)
	if view.comOwned {
		view.comOwned = false
		windows.CoUninitialize()
	}
}
