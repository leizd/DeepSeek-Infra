//go:build darwin && !ios

package desktop

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"structs"
	"sync"
	"unsafe"

	"github.com/ebitengine/purego"
	"github.com/ebitengine/purego/objc"
)

type macPoint struct {
	_    structs.HostLayout
	X, Y float64
}
type macSize struct {
	_             structs.HostLayout
	Width, Height float64
}
type macRect struct {
	_      structs.HostLayout
	Origin macPoint
	Size   macSize
}

// Layout is the public Apple Blocks ABI. Cocoa supplies these blocks; purego's
// Block.Invoke supports its own Go-created blocks only, so call the native
// invoke pointer with the original block as the first argument.
// https://clang.llvm.org/docs/Block-ABI-Apple.html
type macBlockHeader struct {
	_                  structs.HostLayout
	isa                uintptr
	flags, reserved    int32
	invoke, descriptor uintptr
}

var macRuntime struct {
	once                 sync.Once
	err                  error
	delegate             objc.Class
	copyBlockHeader      func(*macBlockHeader, uintptr, uintptr) uintptr
	protocolAvailability map[string]bool
}
var macWindows sync.Map

type macDownload struct {
	object                 objc.ID
	temporary, destination string
}
type macWindow struct {
	app, host, browser, delegate, pool                  objc.ID
	mu                                                  sync.Mutex
	queue                                               []func()
	closed                                              bool
	closeGuard                                          func() bool
	runErr                                              error
	downloads                                           map[objc.ID]macDownload
	released                                            bool
	navigationActions, navigationResponses, queueDrains uint64
}

func macSEL(name string) objc.SEL  { return objc.RegisterName(name) }
func macClass(name string) objc.ID { return objc.ID(objc.GetClass(name)) }
func macString(value string) objc.ID {
	return macClass("NSString").Send(macSEL("stringWithUTF8String:"), value)
}
func macText(value objc.ID) string {
	if value == 0 {
		return ""
	}
	return objc.Send[string](value, macSEL("UTF8String"))
}
func macView(self objc.ID) *macWindow {
	value, ok := macWindows.Load(self)
	if !ok {
		return nil
	}
	return value.(*macWindow)
}

func loadMacRuntime() error {
	macRuntime.once.Do(func() {
		for _, library := range []string{"/System/Library/Frameworks/Cocoa.framework/Cocoa", "/System/Library/Frameworks/WebKit.framework/WebKit"} {
			if _, err := purego.Dlopen(library, purego.RTLD_GLOBAL|purego.RTLD_NOW); err != nil {
				macRuntime.err = errors.New("native macOS framework unavailable")
				return
			}
		}
		library, err := purego.Dlopen("/usr/lib/libSystem.B.dylib", purego.RTLD_LOCAL|purego.RTLD_NOW)
		if err != nil {
			macRuntime.err = errors.New("native macOS system API unavailable")
			return
		}
		pointer, err := purego.Dlsym(library, "memcpy")
		if err != nil {
			macRuntime.err = errors.New("native macOS block API unavailable")
			return
		}
		purego.RegisterFunc(&macRuntime.copyBlockHeader, pointer)
		for _, name := range []string{"NSObject", "NSApplication", "NSWindow", "WKWebView", "WKWebsiteDataStore", "WKWebViewConfiguration", "WKDownload", "NSOpenPanel", "NSSavePanel", "NSAlert"} {
			if objc.GetClass(name) == 0 {
				macRuntime.err = errors.New("native macOS class unavailable: " + name)
				return
			}
		}
		protocols := make([]*objc.Protocol, 0, 5)
		macRuntime.protocolAvailability = make(map[string]bool)
		for _, name := range []string{"NSWindowDelegate", "NSApplicationDelegate", "WKUIDelegate", "WKNavigationDelegate", "WKDownloadDelegate"} {
			protocol := objc.GetProtocol(name)
			macRuntime.protocolAvailability[name] = protocol != nil
			// Formal protocol metadata need not be published by a framework.
			// Register every callback regardless; WebKit dispatches by selector.
			// https://github.com/WebKit/WebKit/blob/main/Source/WebKit/UIProcess/Cocoa/UIDelegate.mm
			if protocol != nil {
				protocols = append(protocols, protocol)
			}
		}
		methods := []objc.MethodDef{
			{Cmd: macSEL("drainNativeQueue:"), Fn: macDrainQueue},
			{Cmd: macSEL("windowShouldClose:"), Fn: macWindowShouldClose},
			{Cmd: macSEL("applicationShouldTerminate:"), Fn: macApplicationShouldTerminate},
			{Cmd: macSEL("webView:runOpenPanelWithParameters:initiatedByFrame:completionHandler:"), Fn: macOpenPanel},
			{Cmd: macSEL("webView:runJavaScriptAlertPanelWithMessage:initiatedByFrame:completionHandler:"), Fn: macJavaScriptAlert},
			{Cmd: macSEL("webView:runJavaScriptConfirmPanelWithMessage:initiatedByFrame:completionHandler:"), Fn: macJavaScriptConfirm},
			{Cmd: macSEL("webView:runJavaScriptTextInputPanelWithPrompt:defaultText:initiatedByFrame:completionHandler:"), Fn: macJavaScriptPrompt},
			{Cmd: macSEL("webView:createWebViewWithConfiguration:forNavigationAction:windowFeatures:"), Fn: macNewWebView},
			{Cmd: macSEL("webView:decidePolicyForNavigationAction:decisionHandler:"), Fn: macNavigationAction},
			{Cmd: macSEL("webView:decidePolicyForNavigationResponse:decisionHandler:"), Fn: macNavigationResponse},
			{Cmd: macSEL("webView:navigationAction:didBecomeDownload:"), Fn: macBecameDownload},
			{Cmd: macSEL("webView:navigationResponse:didBecomeDownload:"), Fn: macBecameDownload},
			{Cmd: macSEL("download:decideDestinationUsingResponse:suggestedFilename:completionHandler:"), Fn: macDownloadDestination},
			{Cmd: macSEL("downloadDidFinish:"), Fn: macDownloadFinished},
			{Cmd: macSEL("download:didFailWithError:resumeData:"), Fn: macDownloadFailed},
			{Cmd: macSEL("webView:didFailProvisionalNavigation:withError:"), Fn: macNavigationFailed},
			{Cmd: macSEL("webView:didFailNavigation:withError:"), Fn: macNavigationFailed},
			{Cmd: macSEL("webViewWebContentProcessDidTerminate:"), Fn: macWebProcessTerminated},
		}
		macRuntime.delegate, macRuntime.err = objc.RegisterClass("DeepSeekNativeWindowDelegateV1", objc.GetClass("NSObject"), protocols, nil, methods)
		if macRuntime.err != nil {
			return
		}
		for _, method := range methods {
			if !objc.Send[bool](objc.ID(macRuntime.delegate), macSEL("instancesRespondToSelector:"), method.Cmd) {
				macRuntime.err = errors.New("native macOS delegate callback unavailable")
				return
			}
		}
	})
	return macRuntime.err
}

func macComplete(block objc.Block, args ...uintptr) {
	if block == 0 {
		return
	}
	var header macBlockHeader
	macRuntime.copyBlockHeader(&header, uintptr(block), unsafe.Sizeof(header))
	if header.invoke == 0 {
		panic("native macOS completion has no invoke pointer")
	}
	parameters := append([]uintptr{uintptr(block)}, args...)
	purego.SyscallN(header.invoke, parameters...)
}

func newPlatformWindow(profile string) (window, error) {
	bundle, err := ensureMacAppBundle(profile)
	if err != nil {
		return nil, err
	}
	if err := loadMacRuntime(); err != nil {
		return nil, err
	}
	if !objc.Send[bool](macClass("NSThread"), macSEL("isMainThread")) {
		return nil, errors.New("native macOS window requires the initial main thread")
	}
	pool := macClass("NSAutoreleasePool").Send(macSEL("new"))
	mainBundle := macClass("NSBundle").Send(macSEL("mainBundle"))
	if macText(mainBundle.Send(macSEL("bundleIdentifier"))) != bundle.identifier {
		pool.Send(macSEL("drain"))
		return nil, errors.New("native macOS profile bundle identity differs")
	}
	w := &macWindow{pool: pool, downloads: make(map[objc.ID]macDownload)}
	ready := false
	defer func() {
		if !ready {
			w.Destroy()
			w.release()
		}
	}()
	w.app = macClass("NSApplication").Send(macSEL("sharedApplication"))
	if w.app == 0 {
		return nil, errors.New("native macOS application unavailable")
	}
	if err := ensureMacRegularActivationPolicy(
		func() int64 { return objc.Send[int64](w.app, macSEL("activationPolicy")) },
		func() bool { return objc.Send[bool](w.app, macSEL("setActivationPolicy:"), int64(0)) },
	); err != nil {
		return nil, err
	}
	w.delegate = objc.ID(macRuntime.delegate).Send(macSEL("new"))
	macWindows.Store(w.delegate, w)
	w.app.Send(macSEL("setDelegate:"), w.delegate)
	frame := macRect{Size: macSize{Width: Width, Height: Height}}
	w.host = macClass("NSWindow").Send(macSEL("alloc")).Send(macSEL("initWithContentRect:styleMask:backing:defer:"), frame, uint64(1|2|4|8), uint64(2), false)
	if w.host == 0 {
		return nil, errors.New("cannot create native macOS window")
	}
	w.host.Send(macSEL("setReleasedWhenClosed:"), false)
	w.host.Send(macSEL("setTitle:"), macString(Title))
	w.host.Send(macSEL("setContentMinSize:"), macSize{Width: MinWidth, Height: MinHeight})
	w.host.Send(macSEL("setDelegate:"), w.delegate)
	configuration := macClass("WKWebViewConfiguration").Send(macSEL("new"))
	defer configuration.Send(macSEL("release"))
	configuration.Send(macSEL("setWebsiteDataStore:"), macClass("WKWebsiteDataStore").Send(macSEL("defaultDataStore")))
	w.browser = macClass("WKWebView").Send(macSEL("alloc")).Send(macSEL("initWithFrame:configuration:"), frame, configuration)
	if w.browser == 0 {
		return nil, errors.New("cannot create native macOS WebKit view")
	}
	w.browser.Send(macSEL("setUIDelegate:"), w.delegate)
	w.browser.Send(macSEL("setNavigationDelegate:"), w.delegate)
	w.browser.Send(macSEL("setAutoresizingMask:"), uint64(2|16))
	w.host.Send(macSEL("setContentView:"), w.browser)
	w.host.Send(macSEL("makeFirstResponder:"), w.browser)
	w.installMenus()
	w.app.Send(macSEL("finishLaunching"))
	w.host.Send(macSEL("center"))
	w.host.Send(macSEL("makeKeyAndOrderFront:"), objc.ID(0))
	w.app.Send(macSEL("activateIgnoringOtherApps:"), true)
	ready = true
	return w, nil
}

// The required result is a regular application, including an already configured
// app bundle. Verify the public getter rather than treating a setter return as
// evidence of a different or missing state.
// https://developer.apple.com/documentation/appkit/nsapplication/activationpolicy()
func ensureMacRegularActivationPolicy(read func() int64, change func() bool) error {
	before := read()
	if before == 0 {
		return nil
	}
	changed := change()
	after := read()
	if after != 0 {
		return fmt.Errorf("cannot activate native macOS application (before=%d after=%d switched=%t)", before, after, changed)
	}
	return nil
}

func (w *macWindow) Navigate(raw string) {
	url := macClass("NSURL").Send(macSEL("URLWithString:"), macString(raw))
	if url == 0 {
		w.runErr = errors.New("invalid native macOS navigation")
		w.Destroy()
		return
	}
	request := macClass("NSURLRequest").Send(macSEL("requestWithURL:"), url)
	w.browser.Send(macSEL("loadRequest:"), request)
}

func (w *macWindow) Dispatch(fn func()) {
	w.mu.Lock()
	if w.closed {
		w.mu.Unlock()
		return
	}
	w.queue = append(w.queue, fn)
	delegate := w.delegate.Send(macSEL("retain"))
	w.mu.Unlock()
	delegate.Send(macSEL("performSelectorOnMainThread:withObject:waitUntilDone:"), macSEL("drainNativeQueue:"), objc.ID(0), false)
	delegate.Send(macSEL("release"))
}

func macDrainQueue(self objc.ID, _ objc.SEL, _ objc.ID) {
	w := macView(self)
	if w == nil {
		return
	}
	w.queueDrains++
	w.mu.Lock()
	pending := w.queue
	w.queue = nil
	w.mu.Unlock()
	for _, fn := range pending {
		w.mu.Lock()
		closed := w.closed
		w.mu.Unlock()
		if closed {
			return
		}
		fn()
	}
}

func (w *macWindow) Destroy() {
	w.mu.Lock()
	if w.closed {
		w.mu.Unlock()
		return
	}
	w.closed = true
	w.queue = nil
	w.mu.Unlock()
	for key, download := range w.downloads {
		download.object.Send(macSEL("setDelegate:"), objc.ID(0))
		download.object.Send(macSEL("cancel:"), objc.Block(0))
		download.object.Send(macSEL("release"))
		_ = os.Remove(download.temporary)
		delete(w.downloads, key)
	}
	if w.browser != 0 {
		w.browser.Send(macSEL("stopLoading"))
		w.browser.Send(macSEL("setUIDelegate:"), objc.ID(0))
		w.browser.Send(macSEL("setNavigationDelegate:"), objc.ID(0))
	}
	if w.host != 0 {
		w.host.Send(macSEL("setDelegate:"), objc.ID(0))
		w.host.Send(macSEL("close"))
	}
	if w.app != 0 {
		w.app.Send(macSEL("stop:"), objc.ID(0))
		// NSApplication.stop returns only after its next event. Wake a loop which
		// is idle, including cancellation without a user-generated mouse event.
		event := macClass("NSEvent").Send(macSEL("otherEventWithType:location:modifierFlags:timestamp:windowNumber:context:subtype:data1:data2:"), uint64(15), macPoint{}, uint64(0), float64(0), int64(0), objc.ID(0), int16(0), int64(0), int64(0))
		w.app.Send(macSEL("postEvent:atStart:"), event, true)
	}
}

func (w *macWindow) Run() error {
	w.mu.Lock()
	closed := w.closed
	w.mu.Unlock()
	if !closed {
		w.app.Send(macSEL("run"))
	}
	w.Destroy()
	w.release()
	return w.runErr
}

func (w *macWindow) release() {
	if w.released {
		return
	}
	w.released = true
	if w.app != 0 {
		w.app.Send(macSEL("setDelegate:"), objc.ID(0))
	}
	macWindows.Delete(w.delegate)
	for _, object := range []objc.ID{w.browser, w.host, w.delegate} {
		if object != 0 {
			object.Send(macSEL("release"))
		}
	}
	if w.pool != 0 {
		w.pool.Send(macSEL("drain"))
	}
}

func (w *macWindow) SetCloseGuard(guard func() bool) { w.closeGuard = guard }
func (w *macWindow) requestClose() {
	if w.closeGuard == nil || w.closeGuard() {
		w.Destroy()
	}
}
func macWindowShouldClose(self objc.ID, _ objc.SEL, _ objc.ID) bool {
	if w := macView(self); w != nil {
		w.requestClose()
	}
	return false
}
func macApplicationShouldTerminate(self objc.ID, _ objc.SEL, _ objc.ID) int64 {
	if w := macView(self); w != nil {
		w.requestClose()
	}
	return 0
}

func (w *macWindow) ConfirmStop() bool {
	alert := macAlert("停止 DeepSeek 服务？", "关闭启动器会停止本次启动的后台服务。", "停止并退出", "取消")
	defer alert.Send(macSEL("release"))
	return objc.Send[int64](alert, macSEL("runModal")) == 1000
}
func macAlert(title, message string, buttons ...string) objc.ID {
	alert := macClass("NSAlert").Send(macSEL("new"))
	alert.Send(macSEL("setMessageText:"), macString(title))
	alert.Send(macSEL("setInformativeText:"), macString(message))
	for _, title := range buttons {
		alert.Send(macSEL("addButtonWithTitle:"), macString(title))
	}
	return alert
}

func (w *macWindow) installMenus() {
	bar := macClass("NSMenu").Send(macSEL("new"))
	defer bar.Send(macSEL("release"))
	for _, section := range []struct {
		title string
		items [][3]string
	}{
		{Title, [][3]string{{"退出 DeepSeek Infra", "terminate:", "q"}}},
		{"编辑", [][3]string{{"撤销", "undo:", "z"}, {"重做", "redo:", "Z"}, {"剪切", "cut:", "x"}, {"复制", "copy:", "c"}, {"粘贴", "paste:", "v"}, {"全选", "selectAll:", "a"}}},
	} {
		item := macClass("NSMenuItem").Send(macSEL("new"))
		menu := macClass("NSMenu").Send(macSEL("alloc")).Send(macSEL("initWithTitle:"), macString(section.title))
		for _, entry := range section.items {
			child := macClass("NSMenuItem").Send(macSEL("alloc")).Send(macSEL("initWithTitle:action:keyEquivalent:"), macString(entry[0]), macSEL(entry[1]), macString(entry[2]))
			menu.Send(macSEL("addItem:"), child)
			child.Send(macSEL("release"))
		}
		item.Send(macSEL("setSubmenu:"), menu)
		bar.Send(macSEL("addItem:"), item)
		menu.Send(macSEL("release"))
		item.Send(macSEL("release"))
	}
	w.app.Send(macSEL("setMainMenu:"), bar)
}

func macOpenPanel(self objc.ID, _ objc.SEL, _ objc.ID, parameters objc.ID, _ objc.ID, completion objc.Block) {
	if macView(self) == nil {
		macComplete(completion, 0)
		return
	}
	panel := macClass("NSOpenPanel").Send(macSEL("openPanel"))
	panel.Send(macSEL("setCanChooseFiles:"), true)
	panel.Send(macSEL("setCanChooseDirectories:"), objc.Send[bool](parameters, macSEL("allowsDirectories")))
	panel.Send(macSEL("setAllowsMultipleSelection:"), objc.Send[bool](parameters, macSEL("allowsMultipleSelection")))
	var result objc.ID
	if objc.Send[int64](panel, macSEL("runModal")) == 1 {
		result = panel.Send(macSEL("URLs"))
	}
	macComplete(completion, uintptr(result))
}
func macJavaScriptAlert(self objc.ID, _ objc.SEL, _ objc.ID, message objc.ID, _ objc.ID, completion objc.Block) {
	if macView(self) != nil {
		alert := macAlert(Title, macText(message), "确定")
		alert.Send(macSEL("runModal"))
		alert.Send(macSEL("release"))
	}
	macComplete(completion)
}
func macJavaScriptConfirm(self objc.ID, _ objc.SEL, _ objc.ID, message objc.ID, _ objc.ID, completion objc.Block) {
	var accepted uintptr
	if macView(self) != nil {
		alert := macAlert(Title, macText(message), "确定", "取消")
		if objc.Send[int64](alert, macSEL("runModal")) == 1000 {
			accepted = 1
		}
		alert.Send(macSEL("release"))
	}
	macComplete(completion, accepted)
}
func macJavaScriptPrompt(self objc.ID, _ objc.SEL, _ objc.ID, prompt, defaultText objc.ID, _ objc.ID, completion objc.Block) {
	var result objc.ID
	if macView(self) != nil {
		alert := macAlert(Title, macText(prompt), "确定", "取消")
		input := macClass("NSTextField").Send(macSEL("alloc")).Send(macSEL("initWithFrame:"), macRect{Size: macSize{Width: 320, Height: 26}})
		input.Send(macSEL("setStringValue:"), macString(macText(defaultText)))
		alert.Send(macSEL("setAccessoryView:"), input)
		if objc.Send[int64](alert, macSEL("runModal")) == 1000 {
			result = input.Send(macSEL("stringValue"))
		}
		macComplete(completion, uintptr(result))
		input.Send(macSEL("release"))
		alert.Send(macSEL("release"))
		return
	}
	macComplete(completion, 0)
}
func macNewWebView(self objc.ID, _ objc.SEL, _ objc.ID, _ objc.ID, action objc.ID, _ objc.ID) objc.ID {
	if w := macView(self); w != nil && action.Send(macSEL("targetFrame")) == 0 {
		w.browser.Send(macSEL("loadRequest:"), action.Send(macSEL("request")))
	}
	return 0
}
func macNavigationAction(self objc.ID, _ objc.SEL, _ objc.ID, action objc.ID, completion objc.Block) {
	w := macView(self)
	if w == nil {
		macComplete(completion, 0)
		return
	}
	w.navigationActions++
	policy := uintptr(1)
	if objc.Send[bool](action, macSEL("shouldPerformDownload")) {
		policy = 2
	}
	macComplete(completion, policy)
}
func macNavigationResponse(self objc.ID, _ objc.SEL, _ objc.ID, response objc.ID, completion objc.Block) {
	w := macView(self)
	if w == nil {
		macComplete(completion, 0)
		return
	}
	w.navigationResponses++
	policy := uintptr(1)
	if !objc.Send[bool](response, macSEL("canShowMIMEType")) {
		policy = 2
	}
	macComplete(completion, policy)
}
func macBecameDownload(self objc.ID, _ objc.SEL, _ objc.ID, _ objc.ID, download objc.ID) {
	if macView(self) != nil {
		download.Send(macSEL("setDelegate:"), self)
	}
}
func macDownloadDestination(self objc.ID, _ objc.SEL, download objc.ID, _ objc.ID, suggested objc.ID, completion objc.Block) {
	w := macView(self)
	if w == nil {
		macComplete(completion, 0)
		return
	}
	panel := macClass("NSSavePanel").Send(macSEL("savePanel"))
	panel.Send(macSEL("setNameFieldStringValue:"), macString(filepath.Base(macText(suggested))))
	panel.Send(macSEL("setCanCreateDirectories:"), true)
	if objc.Send[int64](panel, macSEL("runModal")) != 1 {
		macComplete(completion, 0)
		return
	}
	destination := macText(panel.Send(macSEL("URL")).Send(macSEL("path")))
	file, err := os.CreateTemp(filepath.Dir(destination), ".deepseek-download-")
	if err != nil {
		macComplete(completion, 0)
		return
	}
	temporary := file.Name()
	if closeErr := file.Close(); closeErr != nil {
		_ = os.Remove(temporary)
		macComplete(completion, 0)
		return
	}
	if err := os.Remove(temporary); err != nil {
		macComplete(completion, 0)
		return
	}
	w.downloads[download] = macDownload{object: download.Send(macSEL("retain")), temporary: temporary, destination: destination}
	url := macClass("NSURL").Send(macSEL("fileURLWithPath:"), macString(temporary))
	macComplete(completion, uintptr(url))
}
func macDownloadFinished(self objc.ID, _ objc.SEL, download objc.ID) {
	w := macView(self)
	if w == nil {
		return
	}
	item, ok := w.downloads[download]
	if !ok {
		return
	}
	delete(w.downloads, download)
	defer item.object.Send(macSEL("release"))
	err := os.Chmod(item.temporary, 0o600)
	if err == nil {
		err = os.Rename(item.temporary, item.destination)
	}
	if err != nil {
		_ = os.Remove(item.temporary)
		alert := macAlert(Title, "无法保存下载文件。", "确定")
		alert.Send(macSEL("runModal"))
		alert.Send(macSEL("release"))
	}
}
func macDownloadFailed(self objc.ID, _ objc.SEL, download objc.ID, _ objc.ID, _ objc.ID) {
	w := macView(self)
	if w == nil {
		return
	}
	if item, ok := w.downloads[download]; ok {
		delete(w.downloads, download)
		_ = os.Remove(item.temporary)
		item.object.Send(macSEL("release"))
	}
	alert := macAlert(Title, "下载未完成，请检查连接后重试。", "确定")
	alert.Send(macSEL("runModal"))
	alert.Send(macSEL("release"))
}
func macNavigationFailed(self objc.ID, _ objc.SEL, _ objc.ID, _ objc.ID, nativeError objc.ID) {
	if w := macView(self); w != nil {
		// NSURLErrorCancelled is expected when a navigation is superseded or
		// converted to a download; it is not a failed application startup.
		if objc.Send[int64](nativeError, macSEL("code")) == -999 {
			return
		}
		w.runErr = errors.New("native macOS WebKit navigation failed")
		w.Destroy()
	}
}
func macWebProcessTerminated(self objc.ID, _ objc.SEL, _ objc.ID) {
	if w := macView(self); w != nil {
		w.runErr = errors.New("native macOS WebKit content process terminated")
		w.Destroy()
	}
}
