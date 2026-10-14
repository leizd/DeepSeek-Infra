//go:build windows

package desktop

import (
	"errors"
	"runtime"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

func openPlatformBrowser(raw string) error {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	err := windows.CoInitializeEx(0, windows.COINIT_APARTMENTTHREADED|windows.COINIT_DISABLE_OLE1DDE)
	if err != nil && err != syscall.Errno(1) { // S_FALSE also owns an init reference.
		return errors.New("default browser initialization failed")
	}
	defer windows.CoUninitialize()
	verb, _ := windows.UTF16PtrFromString("open")
	target, err := windows.UTF16PtrFromString(raw)
	if err != nil {
		return errors.New("invalid native browser URL")
	}
	// ShellExecute's return code is authoritative even when GetLastError is zero.
	open := windows.NewLazySystemDLL("shell32.dll").NewProc("ShellExecuteW")
	result, _, _ := open.Call(0, uintptr(unsafe.Pointer(verb)), uintptr(unsafe.Pointer(target)), 0, 0, windows.SW_SHOWNORMAL)
	if result <= 32 {
		return errors.New("default browser unavailable")
	}
	return nil
}
