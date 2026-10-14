//go:build windows

package launcherconfig

import (
	"errors"
	"os"
	"runtime"
	"unsafe"

	"golang.org/x/sys/windows"
)

const protectionName = "windows-dpapi-user-v1"
const settingsAAD = "deepseek-native-launcher-settings/v2"

func protect(value []byte, decrypt bool) ([]byte, error) {
	if len(value) == 0 || len(value) > maxSettingsBytes {
		return nil, errors.New("invalid launcher ciphertext")
	}
	entropy := []byte(settingsAAD)
	input := windows.DataBlob{Size: uint32(len(value)), Data: &value[0]}
	additional := windows.DataBlob{Size: uint32(len(entropy)), Data: &entropy[0]}
	var output windows.DataBlob
	var err error
	if decrypt {
		err = windows.CryptUnprotectData(&input, nil, &additional, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &output)
	} else {
		err = windows.CryptProtectData(&input, nil, &additional, 0, nil, windows.CRYPTPROTECT_UI_FORBIDDEN, &output)
	}
	runtime.KeepAlive(value)
	runtime.KeepAlive(entropy)
	if err != nil {
		return nil, errors.New("launcher operating system protection failed")
	}
	defer windows.LocalFree(windows.Handle(unsafe.Pointer(output.Data)))
	if output.Data == nil || output.Size == 0 || output.Size > maxSettingsBytes {
		return nil, errors.New("invalid launcher protected data")
	}
	result := append([]byte(nil), unsafe.Slice(output.Data, int(output.Size))...)
	if decrypt {
		clear(unsafe.Slice(output.Data, int(output.Size)))
	}
	return result, nil
}

func seal(plain []byte, _ *os.Root) ([]byte, error)  { return protect(plain, false) }
func unseal(data []byte, _ *os.Root) ([]byte, error) { return protect(data, true) }
