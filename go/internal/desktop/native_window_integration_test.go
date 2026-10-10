//go:build windows && native_desktop_integration

package desktop

import (
	"context"
	"encoding/json"
	"net/url"
	"os"
	"testing"
	"time"

	"github.com/wailsapp/go-webview2/pkg/edge"
)

// This explicit platform gate loads the real Vite app in the real WebView2
// control and performs a nonempty upload/read/source workload in that browser.
// Its temporary observation callback is absent from the production host.
func TestNativeWindowLoadsAuthenticatedReactAndRunsFileWorkload(t *testing.T) {
	entry := os.Getenv("DEEPSEEK_DESKTOP_INTEGRATION_URL")
	parsed, err := url.Parse(entry)
	if err != nil || parsed.Scheme != "http" || parsed.Hostname() != "127.0.0.1" || parsed.Query().Get("token") == "" {
		t.Fatal("explicit isolated Rust gateway launch URL is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	result := make(chan map[string]any, 1)
	profile := os.Getenv("DEEPSEEK_DESKTOP_INTEGRATION_PROFILE")
	if profile == "" {
		profile = t.TempDir()
	}
	err = show(ctx, entry, profile, func(profile string) (window, error) {
		created, err := newPlatformWindow(profile)
		if err != nil {
			return nil, err
		}
		view := created.(*nativeWindow)
		settings, err := view.browser.GetSettings()
		if err != nil {
			return nil, err
		}
		if enabled, err := settings.GetIsWebMessageEnabled(); err != nil || enabled {
			t.Error("production host messages were not disabled")
		}
		if err := settings.PutIsWebMessageEnabled(true); err != nil {
			return nil, err
		}
		view.browser.MessageCallback = func(message string, _ *edge.ICoreWebView2, args *edge.ICoreWebView2WebMessageReceivedEventArgs) {
			source, err := args.GetSource()
			actual, parseErr := url.Parse(source)
			if err != nil || parseErr != nil || actual.Host != parsed.Host {
				t.Error("test observation came from a different origin")
				return
			}
			var receipt map[string]any
			if err := json.Unmarshal([]byte(message), &receipt); err != nil {
				t.Error("invalid platform observation")
				return
			}
			select {
			case result <- receipt:
			default:
			}
			view.Dispatch(view.Destroy)
		}
		view.browser.Init(`window.addEventListener('DOMContentLoaded',async()=>{
            const receipt={};
            try {
                for(let attempt=0;attempt<100&&!document.querySelector('textarea');attempt++) await new Promise(resolve=>setTimeout(resolve,100));
                receipt.react=document.body.innerText.includes('DeepSeek')&&document.querySelectorAll('button').length>=8;
                receipt.desktop=new URL(location.href).searchParams.get('desktop')==='1';
                receipt.configStatus=(await fetch('/api/config')).status;
                const original='Native desktop upload 2026\n原生桌面阅读验证\nA nonempty retained file workload.';
                const form=new FormData();form.append('files',new File([original],'native-desktop.txt',{type:'text/plain'}));form.append('ocrEnabled','false');
                const uploaded=await fetch('/api/file-text',{method:'POST',body:form});receipt.uploadStatus=uploaded.status;
                const body=await uploaded.json();const file=(body.files||[])[0];receipt.fileId=!!file?.fileId;
                if(file?.fileId){
                    const reader=await fetch('/api/file-reader',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fileId:file.fileId})});
                    receipt.readerStatus=reader.status;receipt.readerText=(await reader.text()).includes('Native desktop upload 2026');
                    const source=await fetch('/api/file-source?fileId='+encodeURIComponent(file.fileId));receipt.sourceStatus=source.status;receipt.originalEqual=(await source.text())===original;
                }
                receipt.layout=innerWidth>=760&&innerHeight>=520;
            } catch(error){receipt.failure=String(error).slice(0,120);}
            window.chrome.webview.postMessage(JSON.stringify(receipt));
        },{once:true});`)
		return view, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case receipt := <-result:
		for _, key := range []string{"react", "desktop", "fileId", "readerText", "originalEqual", "layout"} {
			if receipt[key] != true {
				t.Errorf("actual browser check %s failed", key)
			}
		}
		for _, key := range []string{"configStatus", "uploadStatus", "readerStatus", "sourceStatus"} {
			if receipt[key] != float64(200) {
				t.Errorf("actual browser status %s = %v", key, receipt[key])
			}
		}
		if path := os.Getenv("DEEPSEEK_DESKTOP_INTEGRATION_REPORT"); path != "" {
			body, _ := json.MarshalIndent(map[string]any{"checks": receipt, "releaseQualified": false, "platform": "windows-amd64-webview2", "profile": profile}, "", "  ")
			if err := os.WriteFile(path, append(body, '\n'), 0o600); err != nil {
				t.Fatal(err)
			}
		}
	case <-ctx.Done():
		t.Fatal("real desktop browser did not complete its workload")
	}
}
