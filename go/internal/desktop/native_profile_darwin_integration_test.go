//go:build darwin && !ios && native_desktop_integration

package desktop

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/ebitengine/purego/objc"
)

type macProfileObservation struct {
	Nonce             string  `json:"nonce"`
	Phase             string  `json:"phase"`
	LocalValue        *string `json:"localValue"`
	IndexedValue      *string `json:"indexedValue"`
	CookieVisible     bool    `json:"cookieVisible"`
	Error             string  `json:"error"`
	CookieMatched     bool    `json:"-"`
	CookiePresent     bool    `json:"-"`
	CookieValueSHA256 string  `json:"-"`
}

type macProfileChildReceipt struct {
	SourceCommit              string `json:"sourceCommit"`
	RunID                     string `json:"runId"`
	Attempt                   string `json:"attempt"`
	Architecture              string `json:"architecture"`
	Phase                     string `json:"phase"`
	PID                       int    `json:"pid"`
	BundleID                  string `json:"bundleIdentifier"`
	ExecutableSHA256          string `json:"actualExecutableSha256"`
	Visible                   bool   `json:"visible"`
	MainThread                bool   `json:"mainThread"`
	RegularPolicy             bool   `json:"regularPolicy"`
	PersistentDataStore       bool   `json:"persistentDataStore"`
	WindowID                  int64  `json:"windowId"`
	ActionCallbacks           uint64 `json:"navigationActionCallbacks"`
	ResponseCallbacks         uint64 `json:"navigationResponseCallbacks"`
	DispatchCallbacks         uint64 `json:"dispatchCallbacks"`
	SignalCancellationSettled bool   `json:"signalCancellationSettled"`
	FullMigrationQualified    bool   `json:"fullMigrationQualified"`
}

// TestMain enters this helper on the initial thread in a new real UI process.
// It is test-only code: production executables contain no fixture/observer mode.
func runMacProfileChild() error {
	profile, entry := os.Getenv("DEEPSEEK_MACOS_PROFILE"), os.Getenv("DEEPSEEK_MACOS_CHILD_URL")
	path, phase := os.Getenv("DEEPSEEK_MACOS_CHILD_REPORT"), os.Getenv("DEEPSEEK_MACOS_CHILD_PHASE")
	if !filepath.IsAbs(profile) || !filepath.IsAbs(path) || (phase != "write" && phase != "read" && phase != "isolated") {
		return errors.New("invalid native Mac child fixture context")
	}
	signalContext, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(signalContext, 45*time.Second)
	defer cancel()
	receipt := macProfileChildReceipt{SourceCommit: os.Getenv("GITHUB_SHA"), RunID: os.Getenv("GITHUB_RUN_ID"),
		Attempt: os.Getenv("GITHUB_RUN_ATTEMPT"), Architecture: runtime.GOARCH, Phase: phase, PID: os.Getpid()}
	var actual *macWindow
	err := show(ctx, entry, profile, func(profile string) (window, error) {
		view, err := newPlatformWindow(profile)
		if err != nil {
			return nil, err
		}
		actual = view.(*macWindow)
		receipt.Visible = objc.Send[bool](actual.host, macSEL("isVisible"))
		receipt.WindowID = objc.Send[int64](actual.host, macSEL("windowNumber"))
		receipt.MainThread = objc.Send[bool](macClass("NSThread"), macSEL("isMainThread"))
		receipt.RegularPolicy = objc.Send[int64](actual.app, macSEL("activationPolicy")) == 0
		store := actual.browser.Send(macSEL("configuration")).Send(macSEL("websiteDataStore"))
		receipt.PersistentDataStore = objc.Send[bool](store, macSEL("isPersistent"))
		receipt.BundleID = macText(macClass("NSBundle").Send(macSEL("mainBundle")).Send(macSEL("bundleIdentifier")))
		return view, nil
	})
	if err != nil {
		return err
	}
	if actual == nil || signalContext.Err() != context.Canceled || ctx.Err() == context.DeadlineExceeded {
		return errors.New("native Mac profile child did not settle its requested signal")
	}
	receipt.ActionCallbacks, receipt.ResponseCallbacks, receipt.DispatchCallbacks = actual.navigationActions, actual.navigationResponses, actual.queueDrains
	receipt.SignalCancellationSettled = true
	if !receipt.Visible || !receipt.MainThread || !receipt.RegularPolicy || !receipt.PersistentDataStore || receipt.WindowID <= 0 || receipt.ActionCallbacks == 0 || receipt.ResponseCallbacks == 0 || receipt.DispatchCallbacks == 0 {
		return errors.New("actual native Mac child window and callbacks were not observed")
	}
	executable, err := os.Executable()
	if err != nil {
		return errors.New("native Mac child executable unavailable")
	}
	bytes, err := os.ReadFile(executable)
	if err != nil {
		return errors.New("native Mac child executable unreadable")
	}
	receipt.ExecutableSHA256 = fmt.Sprintf("%x", sha256.Sum256(bytes))
	bytes, err = json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(bytes, '\n'), 0o600)
}

func TestNativeMacPersistentProfilesAcrossActualUIRestarts(t *testing.T) {
	if os.Getenv("GITHUB_ACTIONS") != "true" || os.Getenv("RUNNER_ENVIRONMENT") != "github-hosted" || os.Getenv("RUNNER_OS") != "macOS" || os.Getenv("GITHUB_REPOSITORY") != "leizd/DeepSeek-Infra" || os.Getenv("GITHUB_EVENT_NAME") != "workflow_dispatch" || runtime.GOARCH != os.Getenv("DEEPSEEK_MACOS_ARCH") {
		t.Fatal("explicit source-qualified native Mac cloud fixture required")
	}
	reportRoot := filepath.Dir(os.Getenv("DEEPSEEK_MACOS_REPORT"))
	if !filepath.IsAbs(reportRoot) {
		t.Fatal("absolute native Mac report directory required")
	}
	var seed [16]byte
	if _, err := rand.Read(seed[:]); err != nil {
		t.Fatal(err)
	}
	value := "native-profile-value-" + hex.EncodeToString(seed[:])
	const cookieName = "native_profile_continuity"
	var registryMu sync.Mutex
	registry, requests := make(map[string]string), make(map[string]int)
	observed := make(chan macProfileObservation, 8)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		nonce := r.URL.Query().Get("token")
		registryMu.Lock()
		phase, ok := registry[nonce]
		registryMu.Unlock()
		if !ok {
			http.Error(w, "unknown native profile fixture", http.StatusUnauthorized)
			return
		}
		if r.URL.Path == "/profile-observed" && r.Method == http.MethodPost {
			var item macProfileObservation
			decoder := json.NewDecoder(io.LimitReader(r.Body, 4097))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&item) != nil || item.Nonce != nonce || item.Phase != phase {
				http.Error(w, "invalid native profile observation", 400)
				return
			}
			cookie, err := r.Cookie(cookieName)
			item.CookiePresent = err == nil
			item.CookieMatched = item.CookiePresent && cookie.Value == value
			if item.CookiePresent {
				item.CookieValueSHA256 = fmt.Sprintf("%x", sha256.Sum256([]byte(cookie.Value)))
			}
			registryMu.Lock()
			requests[nonce]++
			registryMu.Unlock()
			select {
			case observed <- item:
			default:
			}
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if r.URL.Path != "/" || r.Method != http.MethodGet {
			http.NotFound(w, r)
			return
		}
		if phase == "write" {
			http.SetCookie(w, &http.Cookie{Name: cookieName, Value: value, Path: "/", MaxAge: 86400, HttpOnly: true, SameSite: http.SameSiteStrictMode})
		}
		writeValue := ""
		if phase == "write" {
			writeValue = value
		}
		config, _ := json.Marshal(map[string]string{"nonce": nonce, "phase": phase, "value": writeValue, "cookie": cookieName})
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = fmt.Fprintf(w, `<!doctype html><html><body><h1>Native profile continuity fixture</h1><script>
const c=%s;
async function database(){return await new Promise((resolve,reject)=>{const r=indexedDB.open('native-profile-fixture',1);r.onupgradeneeded=()=>r.result.createObjectStore('kv');r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(Error('database-open'));});}
async function put(db){await new Promise((resolve,reject)=>{const tx=db.transaction('kv','readwrite');tx.objectStore('kv').put(c.value,'continuity');tx.oncomplete=resolve;tx.onerror=()=>reject(Error('database-write'));tx.onabort=()=>reject(Error('database-abort'));});}
async function get(db){return await new Promise((resolve,reject)=>{const r=db.transaction('kv').objectStore('kv').get('continuity');r.onsuccess=()=>resolve(r.result??null);r.onerror=()=>reject(Error('database-read'));});}
async function run(){const db=await database();if(c.phase==='write'){localStorage.setItem('native-profile-continuity',c.value);await put(db);}const indexedValue=await get(db);db.close();await fetch('/profile-observed?token='+c.nonce,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nonce:c.nonce,phase:c.phase,localValue:localStorage.getItem('native-profile-continuity'),indexedValue,cookieVisible:document.cookie.includes(c.cookie+'='),error:''})});}
run().catch(()=>fetch('/profile-observed?token='+c.nonce,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nonce:c.nonce,phase:c.phase,localValue:null,indexedValue:null,cookieVisible:false,error:'native-store-operation-failed'})}));
</script></body></html>`, config)
	}))
	defer server.Close()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	parentBytes, err := os.ReadFile(executable)
	if err != nil {
		t.Fatal(err)
	}
	expectedHash := fmt.Sprintf("%x", sha256.Sum256(parentBytes))
	profileRoot := t.TempDir()
	var children []macProfileChildReceipt
	var browserProof []map[string]any
	for _, phase := range []string{"write", "read", "isolated"} {
		if _, err := rand.Read(seed[:]); err != nil {
			t.Fatal(err)
		}
		nonce := hex.EncodeToString(seed[:])
		registryMu.Lock()
		registry[nonce] = phase
		registryMu.Unlock()
		profile := filepath.Join(profileRoot, "app")
		if phase == "isolated" {
			profile = filepath.Join(profileRoot, "launcher")
		}
		path := filepath.Join(reportRoot, "profile-"+phase+"-child.json")
		log, err := os.OpenFile(filepath.Join(reportRoot, "profile-"+phase+"-child.log"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
		command := exec.CommandContext(ctx, executable, "-test.run=^$", "-test.timeout=60s")
		for _, entry := range os.Environ() {
			key, _, _ := strings.Cut(entry, "=")
			if key != "DEEPSEEK_MACOS_PROFILE" && !strings.HasPrefix(key, "DEEPSEEK_MACOS_CHILD_") && key != "DEEPSEEK_MACOS_PROFILE_CHILD" {
				command.Env = append(command.Env, entry)
			}
		}
		command.Env = append(command.Env, "DEEPSEEK_MACOS_PROFILE_CHILD=1", "DEEPSEEK_MACOS_PROFILE="+profile, "DEEPSEEK_MACOS_CHILD_PHASE="+phase,
			"DEEPSEEK_MACOS_CHILD_REPORT="+path, "DEEPSEEK_MACOS_CHILD_URL="+server.URL+"/?desktop=1&token="+nonce)
		command.Stdout, command.Stderr = log, log
		command.WaitDelay = time.Second
		if err := command.Start(); err != nil {
			cancel()
			_ = log.Close()
			t.Fatal(err)
		}
		done := make(chan error, 1)
		go func() { done <- command.Wait() }()
		// A test failure cancels only this disposable child; no state is retried.
		t.Cleanup(cancel)
		var item macProfileObservation
		select {
		case item = <-observed:
		case err := <-done:
			cancel()
			_ = log.Close()
			t.Fatal("native profile child ended before its DOM observation", phase, err)
		case <-ctx.Done():
			_ = log.Close()
			t.Fatal("native profile DOM operation timed out", phase)
		}
		if item.Nonce != nonce || item.Phase != phase || item.Error != "" || item.CookieVisible {
			cancel()
			_ = log.Close()
			t.Fatal("native profile observation differs", phase)
		}
		if phase == "isolated" {
			if item.LocalValue != nil || item.IndexedValue != nil || item.CookiePresent {
				cancel()
				_ = log.Close()
				t.Fatal("native profiles share browser state")
			}
		} else if item.LocalValue == nil || *item.LocalValue != value || item.IndexedValue == nil || *item.IndexedValue != value || !item.CookieMatched {
			cancel()
			_ = log.Close()
			t.Fatal("native profile lost committed browser state", phase)
		}
		if err := command.Process.Signal(syscall.SIGTERM); err != nil {
			cancel()
			_ = log.Close()
			t.Fatal(err)
		}
		select {
		case err := <-done:
			if err != nil {
				cancel()
				_ = log.Close()
				t.Fatal("native profile child did not exit cleanly", phase, err)
			}
		case <-ctx.Done():
			_ = log.Close()
			t.Fatal("native profile child did not settle shutdown", phase)
		}
		cancel()
		if err := log.Close(); err != nil {
			t.Fatal(err)
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var receipt macProfileChildReceipt
		if json.Unmarshal(raw, &receipt) != nil || receipt.SourceCommit != os.Getenv("GITHUB_SHA") || receipt.RunID != os.Getenv("GITHUB_RUN_ID") || receipt.Attempt != os.Getenv("GITHUB_RUN_ATTEMPT") || receipt.Architecture != runtime.GOARCH || receipt.Phase != phase || receipt.PID != command.Process.Pid || receipt.ExecutableSHA256 != expectedHash || !receipt.SignalCancellationSettled || receipt.FullMigrationQualified {
			t.Fatal("native profile child source, executable or supervised PID differs", phase)
		}
		registryMu.Lock()
		count := requests[nonce]
		registryMu.Unlock()
		if count != 1 {
			t.Fatal("native profile observed repeated requests", phase, count)
		}
		children = append(children, receipt)
		var localHash, indexedHash, cookieHash any
		if item.LocalValue != nil {
			localHash = fmt.Sprintf("%x", sha256.Sum256([]byte(*item.LocalValue)))
		}
		if item.IndexedValue != nil {
			indexedHash = fmt.Sprintf("%x", sha256.Sum256([]byte(*item.IndexedValue)))
		}
		if item.CookiePresent {
			cookieHash = item.CookieValueSHA256
		}
		browserProof = append(browserProof, map[string]any{"phase": phase, "singleOriginalDOMReport": true,
			"localStorageValuePresent": item.LocalValue != nil, "indexedDBValuePresent": item.IndexedValue != nil,
			"httpOnlyCookiePresent": item.CookiePresent, "localStorageValueSha256": localHash, "indexedDBValueSha256": indexedHash,
			"httpOnlyCookieValueSha256": cookieHash, "expectedProfileStateMatched": true, "cookieHiddenFromJavaScript": true,
			"newNativeUIProcessExitedCleanly": true, "supervisedPIDRetainedAcrossBundleExec": true})
	}
	if children[0].BundleID != children[1].BundleID || children[0].BundleID == children[2].BundleID {
		t.Fatal("native profile bundle identities are not stable and isolated")
	}
	result := map[string]any{"status": "PASS", "sourceCommit": os.Getenv("GITHUB_SHA"), "runId": os.Getenv("GITHUB_RUN_ID"), "attempt": os.Getenv("GITHUB_RUN_ATTEMPT"),
		"expectedControlledValueSha256": fmt.Sprintf("%x", sha256.Sum256([]byte(value))),
		"architecture":                  runtime.GOARCH, "actualNativeUIProcessStarts": 3, "children": children, "browserObservations": browserProof,
		"actualWebKitStateAcrossUIRestartsQualified": true, "actualDistinctProfileIsolationQualified": true, "physicalPowerLossQualified": false,
		"legacyProfileMigrationQualified": false, "actualExecutableUpgradeHistoryQualified": false, "fullRustGoProductWorkloadQualified": false, "allDesktopFeaturesQualified": false, "fullMigrationQualified": false, "readiness": "NOT_READY"}
	raw, err := json.MarshalIndent(result, "", "  ")
	if err != nil || os.WriteFile(filepath.Join(reportRoot, "profile-persistence.json"), append(raw, '\n'), 0o600) != nil {
		t.Fatal("cannot retain native profile restart evidence")
	}
}
