//go:build linux && !android && !deepseek_android

package desktop

import (
	"errors"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ebitengine/purego"
)

func TestGIOBrowserRequiresAbsoluteSystemLibrariesAndAvailableAPIs(t *testing.T) {
	for _, architecture := range []string{"amd64", "arm64", "unsupported"} {
		for _, failure := range []string{"", "library", "symbol"} {
			closed, registered := 0, 0
			access := gtkLibraryAccess{
				open: func(path string, flags int) (uintptr, error) {
					if !filepath.IsAbs(path) || !strings.HasSuffix(path, "/libgio-2.0.so.0") || flags != purego.RTLD_NOW|purego.RTLD_LOCAL {
						t.Fatal("untrusted browser system library path")
					}
					if failure == "library" {
						return 0, errors.New("missing runtime")
					}
					return 17, nil
				},
				symbol: func(uintptr, string) (uintptr, error) {
					if failure == "symbol" {
						return 0, errors.New("incompatible runtime")
					}
					return 19, nil
				},
				register: func(any, uintptr) { registered++ },
			}
			api, err := loadGIOFor(architecture, access, func(uintptr) error { closed++; return nil })
			if architecture == "unsupported" || failure != "" {
				if api != nil || err == nil || registered != 0 {
					t.Fatal("unavailable system browser API was admitted")
				}
				if failure == "symbol" && architecture != "unsupported" && closed != 1 {
					t.Fatal("incompatible browser library reference leaked")
				}
			} else {
				if err != nil || registered != 4 {
					t.Fatal("required native browser APIs missing", err)
				}
				api.release()
				if closed != 1 {
					t.Fatal("browser library reference was not released")
				}
			}
		}
	}
}

func TestGIOExternalBrowserKeepsSessionAndExcludesBackendCredentials(t *testing.T) {
	for _, failure := range []string{"", "context", "launch"} {
		removed := map[string]bool{}
		unreferenced := false
		api := &gioAPI{
			contextNew: func() uintptr {
				if failure == "context" {
					return 0
				}
				return 23
			},
			unset: func(context uintptr, key string) { removed[key] = true },
			launch: func(raw string, context, errorOut uintptr) int32 {
				if raw != "http://127.0.0.1:8000/" || context != 23 || errorOut != 0 {
					t.Fatal("native browser target or ownership changed")
				}
				if failure == "launch" {
					return 0
				}
				return 1
			},
			unref: func(context uintptr) { unreferenced = true },
		}
		err := openGIOBrowser("http://127.0.0.1:8000/", api, []string{"DISPLAY=:99", "HOME=/owned", "DEEPSEEK_API_KEY=private", "AUTH_TOKEN=private", "DEEPSEEKD_INTERNAL_BEARER=private", "PYTHONPATH=/foreign"})
		if (err != nil) != (failure != "") {
			t.Fatal("OS browser result was fabricated", err)
		}
		if failure != "context" && (!unreferenced || !removed["DEEPSEEK_API_KEY"] || !removed["AUTH_TOKEN"] || !removed["DEEPSEEKD_INTERNAL_BEARER"] || !removed["PYTHONPATH"] || removed["DISPLAY"] || removed["HOME"]) {
			t.Fatal("browser context leaked secrets or lost its system session")
		}
	}
}
