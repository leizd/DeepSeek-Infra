//go:build linux && !android && !deepseek_android && native_desktop_integration

package desktop

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"testing"
	"time"
	"unsafe"

	"github.com/ebitengine/purego"
)

// Evaluation is linked only into this explicit test binary. The production
// GTK host installs no script-message handler or JavaScript business bridge.
func TestNativeGTKLoadsAuthenticatedReactAndRunsFileWorkload(t *testing.T) {
	privateURL := os.Getenv("DEEPSEEK_DESKTOP_INTEGRATION_URL")
	u, err := url.Parse(privateURL)
	if err != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.Query().Get("token") == "" {
		t.Fatal("explicit isolated Rust gateway launch URL is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	result := make(chan map[string]any, 1)
	profile := os.Getenv("DEEPSEEK_DESKTOP_INTEGRATION_PROFILE")
	if profile == "" {
		profile = t.TempDir()
	}
	err = show(ctx, privateURL, profile, func(profile string) (window, error) {
		created, err := newPlatformWindow(profile)
		if err != nil {
			return nil, err
		}
		view := created.(*gtkWindow)
		var evaluate func(uintptr, string, int64, uintptr, uintptr, uintptr, uintptr, uintptr, uintptr)
		var finish func(uintptr, uintptr, *uintptr) uintptr
		var asString func(uintptr) unsafe.Pointer
		var free func(unsafe.Pointer)
		for _, binding := range []struct {
			name   string
			target any
		}{
			{"webkit_web_view_call_async_javascript_function", &evaluate},
			{"webkit_web_view_call_async_javascript_function_finish", &finish},
			{"jsc_value_to_string", &asString},
			{"g_free", &free},
		} {
			symbol, err := purego.Dlsym(purego.RTLD_DEFAULT, binding.name)
			if err != nil {
				view.Destroy()
				return nil, errors.New("missing explicit WebKit test API")
			}
			purego.RegisterFunc(binding.target, symbol)
		}
		callback := purego.NewCallback(func(object, asyncResult, data uintptr) {
			var failure uintptr
			value := finish(object, asyncResult, &failure)
			receipt := map[string]any{}
			if value == 0 || failure != 0 {
				receipt["failure"] = "WebKit test evaluation failed"
			} else {
				text := asString(value)
				if text != nil {
					bytes := unsafe.Slice((*byte)(text), 16384)
					for i, b := range bytes {
						if b == 0 {
							_ = json.Unmarshal(bytes[:i], &receipt)
							break
						}
					}
					free(text)
				}
				view.api.unref(value)
			}
			select {
			case result <- receipt:
			default:
			}
			view.Dispatch(view.Destroy)
		})
		started := false
		view.api.signalConnect(view.browser, "load-changed", purego.NewCallback(func(browser uintptr, event int32, data uintptr) {
			if event != 3 || started {
				return
			}
			started = true
			evaluate(browser, `
                const receipt={};
                try {
                    for(let attempt=0;attempt<100&&!document.querySelector('textarea');attempt++) await new Promise(resolve=>setTimeout(resolve,100));
                    receipt.react=document.body.innerText.includes('DeepSeek')&&document.querySelectorAll('button').length>=8;
                    receipt.desktop=new URL(location.href).searchParams.get('desktop')==='1';
                    receipt.configStatus=(await fetch('/api/config')).status;
                    const original='Native GTK desktop upload 2026\n原生桌面阅读验证\nA nonempty retained file workload.';
                    const form=new FormData();form.append('files',new File([original],'native-gtk-desktop.txt',{type:'text/plain'}));form.append('ocrEnabled','false');
                    const uploaded=await fetch('/api/file-text',{method:'POST',body:form});receipt.uploadStatus=uploaded.status;
                    const body=await uploaded.json();const file=(body.files||[])[0];receipt.fileId=!!file?.fileId;
                    if(file?.fileId){
                        const reader=await fetch('/api/file-reader',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fileId:file.fileId})});
                        receipt.readerStatus=reader.status;receipt.readerText=(await reader.text()).includes('Native GTK desktop upload 2026');
                        const source=await fetch('/api/file-source?fileId='+encodeURIComponent(file.fileId));receipt.sourceStatus=source.status;receipt.originalEqual=(await source.text())===original;
                    }
                    receipt.layout=innerWidth>=760&&innerHeight>=520;
					receipt.httpOnlySession=!document.cookie.split(';').some(cookie=>cookie.trim().startsWith('auth_token='));
                } catch(error){receipt.failure=String(error).slice(0,120);}
                return JSON.stringify(receipt);`, -1, 0, 0, 0, 0, callback, 0)
		}), 0, 0, 0)
		return view, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case receipt := <-result:
		for _, key := range []string{"react", "desktop", "fileId", "readerText", "originalEqual", "layout", "httpOnlySession"} {
			if receipt[key] != true {
				t.Errorf("actual GTK browser check %s failed", key)
			}
		}
		for _, key := range []string{"configStatus", "uploadStatus", "readerStatus", "sourceStatus"} {
			if receipt[key] != float64(200) {
				t.Errorf("actual GTK browser status %s = %v", key, receipt[key])
			}
		}
		if path := os.Getenv("DEEPSEEK_DESKTOP_INTEGRATION_REPORT"); path != "" {
			body, _ := json.MarshalIndent(map[string]any{"checks": receipt, "releaseQualified": false, "platform": "linux-amd64-webkitgtk", "profile": profile}, "", "  ")
			if err := os.WriteFile(path, append(body, '\n'), 0o600); err != nil {
				t.Fatal(err)
			}
		}
	case <-ctx.Done():
		t.Fatal("real GTK browser did not complete its workload")
	}
}
