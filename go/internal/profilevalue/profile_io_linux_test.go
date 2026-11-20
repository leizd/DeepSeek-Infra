//go:build linux

package profilevalue

import (
	"context"
	"errors"
	"flag"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

func profileCoverageArguments() []string {
	// Successful child fault probes emit their real counters into the same
	// instrumented test run. A SIGKILL child cannot emit counters and is never
	// represented by synthetic coverage.
	if dir := flag.Lookup("test.gocoverdir"); dir != nil && dir.Value.String() != "" {
		return []string{"-test.gocoverdir=" + dir.Value.String()}
	}
	return nil
}

func denyProfileDescriptorIO(read bool) error {
	filter := []unix.SockFilter{{Code: unix.BPF_LD | unix.BPF_W | unix.BPF_ABS}}
	if read {
		filter = append(filter,
			unix.SockFilter{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: unix.SYS_READ, Jf: 3},
			unix.SockFilter{Code: unix.BPF_LD | unix.BPF_W | unix.BPF_ABS, K: 16},
			unix.SockFilter{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: 3, Jf: 1},
			unix.SockFilter{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ERRNO | uint32(unix.EIO)},
		)
	} else {
		filter = append(filter,
			unix.SockFilter{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: unix.SYS_FSYNC, Jf: 1},
			unix.SockFilter{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ERRNO | uint32(unix.EIO)},
		)
	}
	filter = append(filter, unix.SockFilter{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ALLOW})
	program := unix.SockFprog{Len: uint16(len(filter)), Filter: &filter[0]}
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		return err
	}
	result, _, errno := unix.Syscall(unix.SYS_SECCOMP, unix.SECCOMP_SET_MODE_FILTER, unix.SECCOMP_FILTER_FLAG_TSYNC, uintptr(unsafe.Pointer(&program)))
	runtime.KeepAlive(program)
	runtime.KeepAlive(filter)
	if errno != 0 {
		return errno
	}
	if result != 0 {
		return unix.EBUSY
	}
	return nil
}

func TestProfileValueKernelIOPeer(t *testing.T) {
	mode := os.Getenv("DEEPSEEK_PROFILE_IO_TEST_MODE")
	if mode == "" {
		return
	}
	// The parent owns this test-only inherited descriptor until this child exits.
	// Pin it on a parked stack so the collector cannot reuse fd3 for coverage IO.
	file := os.NewFile(3, "profile-io-fixture")
	info, err := file.Stat()
	if err != nil {
		t.Fatal(err)
	}
	go func(file *os.File) { <-make(chan struct{}); runtime.KeepAlive(file) }(file)
	if err := denyProfileDescriptorIO(mode == "read"); err != nil {
		t.Fatal(err)
	}
	if mode == "read" {
		if _, err := file.Read(make([]byte, 1)); !errors.Is(err, unix.EIO) {
			t.Fatal("actual descriptor read failure unavailable", err)
		}
		if _, err := readRegularDescriptor(file, info, 64); !errors.Is(err, ErrInvalid) {
			t.Fatal("failed descriptor bytes were admitted", err)
		}
	} else if mode == "sync" {
		path := os.Getenv("DEEPSEEK_PROFILE_IO_TEST_PATH")
		if err := Publish(path, []byte(strings.Repeat("a", 32))); !errors.Is(err, unix.EIO) {
			t.Fatal("actual record sync failure was not retained", err)
		}
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatal("unsynced bytes acquired an authoritative head", err)
		}
	} else {
		t.Fatal("unknown IO fixture mode")
	}
}

func profileIOChild(t *testing.T, path, mode string) {
	t.Helper()
	probe := filepath.Join(filepath.Dir(path), "io-probe")
	if err := os.WriteFile(probe, []byte(strings.Repeat("a", 32)), 0o600); err != nil {
		t.Fatal(err)
	}
	file, err := os.Open(probe)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	args := append([]string{"-test.run=^TestProfileValueKernelIOPeer$"}, profileCoverageArguments()...)
	child := exec.CommandContext(ctx, executable, args...)
	child.ExtraFiles = []*os.File{file}
	child.Env = append(os.Environ(), "DEEPSEEK_PROFILE_IO_TEST_MODE="+mode, "DEEPSEEK_PROFILE_IO_TEST_PATH="+path)
	if output, err := child.CombinedOutput(); err != nil {
		t.Fatalf("actual IO peer failed: %v\n%s", err, output)
	}
}

func TestProfileValueRejectsChangedIdentityAndActualReadFailure(t *testing.T) {
	path := profilePath(t)
	value := []byte(strings.Repeat("a", 32))
	if err := os.WriteFile(path, value, 0o600); err != nil {
		t.Fatal(err)
	}
	before, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(path, path+".retained-original"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, value, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readRegular(path, before, 64); !errors.Is(err, ErrInvalid) {
		t.Fatal("replacement inode with identical bytes was admitted", err)
	}
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = file.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := readRegularDescriptor(file, before, 64); !errors.Is(err, ErrInvalid) {
		t.Fatal("closed descriptor was admitted", err)
	}
	profileIOChild(t, profilePath(t), "read")
}

func TestProfileValueFailedRecordSyncNeverPublishesAHead(t *testing.T) {
	path := profilePath(t)
	profileIOChild(t, path, "sync")
	if _, err := Read(path, 64); !os.IsNotExist(err) {
		t.Fatal("failed record sync published a head", err)
	}
	if err := Publish(path, []byte(strings.Repeat("b", 32))); err != nil {
		t.Fatal("restored IO could not publish before admission", err)
	}
	if raw, err := Read(path, 64); err != nil || string(raw) != strings.Repeat("b", 32) {
		t.Fatal("restored IO admitted stale partial bytes", err)
	}
}
