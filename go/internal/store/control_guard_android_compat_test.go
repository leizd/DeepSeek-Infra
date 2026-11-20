//go:build linux && (android || deepseek_android) && sqlite3_flock

package store

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

func TestNativeControlFileGuardPeer(t *testing.T) {
	path := os.Getenv("DEEPSEEK_STORE_GUARD_TEST_PATH")
	if path == "" {
		return
	}
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	lock := unix.Flock_t{Type: unix.F_WRLCK, Whence: 0, Start: 0, Len: 0}
	if err := unix.FcntlFlock(file.Fd(), unix.F_GETLK, &lock); err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(lock.Pid)
	fmt.Println("NATIVE_CONTROL_GUARD_OWNER=" + string(body))
}

func TestAndroidNativeOriginRetainsProcessOwnedControlFileGuard(t *testing.T) {
	path := t.TempDir()
	opts, peer := nativeFixture(t, path)
	control, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	intent, err := os.ReadFile(filepath.Join(path, "native-origin-intent.pb"))
	if err != nil {
		t.Fatal(err)
	}
	probe := func() int32 {
		child := exec.Command(os.Args[0], "-test.run=^TestNativeControlFileGuardPeer$")
		child.Env = append(os.Environ(), "DEEPSEEK_STORE_GUARD_TEST_PATH="+filepath.Join(path, ControlDatabaseFilename))
		body, err := child.CombinedOutput()
		if err != nil {
			t.Fatalf("kernel guard probe: %v %s", err, body)
		}
		for _, line := range strings.Split(string(body), "\n") {
			if strings.HasPrefix(line, "NATIVE_CONTROL_GUARD_OWNER=") {
				var pid int32
				if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "NATIVE_CONTROL_GUARD_OWNER=")), &pid); err != nil {
					t.Fatal(err)
				}
				return pid
			}
		}
		t.Fatalf("no guard probe result: %s", body)
		return -1
	}
	if pid := probe(); pid != int32(os.Getpid()) {
		t.Fatalf("actual native store did not hold its process guard: %d", pid)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if pid := probe(); pid != 0 {
		t.Fatalf("closed native store retained another writer's exclusion: %d", pid)
	}
	retained, err := os.ReadFile(filepath.Join(path, "native-origin-intent.pb"))
	if err != nil || string(retained) != string(intent) {
		t.Fatal("file guard rewrote the original signed creation intent", err)
	}
	if peer.state.Origin == nil {
		t.Fatal("file guard discarded the original Rust-origin receipt")
	}
}
