//go:build linux && (android || deepseek_android)

package launch

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

	"github.com/leizd/DeepSeek-Infra/go/internal/profilevalue"
	"golang.org/x/sys/unix"
)

func TestAndroidProfileValueSyncFailurePeer(t *testing.T) {
	root := os.Getenv("DEEPSEEK_PROFILE_SYNC_TEST_ROOT")
	if root == "" {
		return
	}
	path := filepath.Join(root, "go-control/native-profile/profile-id")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := profilevalue.Publish(path, []byte(strings.Repeat("a", 32))); err != nil {
		t.Fatal(err)
	}
	filter := []unix.SockFilter{
		{Code: unix.BPF_LD | unix.BPF_W | unix.BPF_ABS},
		{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: unix.SYS_FSYNC, Jf: 1},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ERRNO | uint32(unix.EIO)},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ALLOW},
	}
	program := unix.SockFprog{Len: uint16(len(filter)), Filter: &filter[0]}
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		t.Fatal(err)
	}
	result, _, errno := unix.Syscall(unix.SYS_SECCOMP, unix.SECCOMP_SET_MODE_FILTER, unix.SECCOMP_FILTER_FLAG_TSYNC, uintptr(unsafe.Pointer(&program)))
	runtime.KeepAlive(program)
	runtime.KeepAlive(filter)
	if errno != 0 || result != 0 {
		t.Fatal("actual sync denial unavailable", errno, result)
	}
	profile, err := openManagedProfile(root)
	if err == nil {
		profile.close()
		t.Fatal("native profile admission ignored failed metadata durability")
	}
	if !errors.Is(err, unix.EIO) {
		t.Fatal("admission failed at unrelated stage", err)
	}
	if _, err := os.Lstat(filepath.Join(root, "rust-worker")); !os.IsNotExist(err) {
		t.Fatal("failed admission created Rust state", err)
	}
	if raw, err := os.ReadFile(path); err != nil || string(raw) != strings.Repeat("a", 32) {
		t.Fatal("failed admission changed original identity", err)
	}
}

func TestAndroidProfileValueAdmissionRequiresDurableMetadata(t *testing.T) {
	root := t.TempDir()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	args := []string{"-test.run=^TestAndroidProfileValueSyncFailurePeer$"}
	if dir := flag.Lookup("test.gocoverdir"); dir != nil && dir.Value.String() != "" {
		args = append(args, "-test.gocoverdir="+dir.Value.String())
	}
	child := exec.CommandContext(ctx, executable, args...)
	child.Env = append(os.Environ(), "DEEPSEEK_PROFILE_SYNC_TEST_ROOT="+root)
	if output, err := child.CombinedOutput(); err != nil {
		t.Fatalf("sync admission peer failed: %v\n%s", err, output)
	}
	path := filepath.Join(root, "go-control/native-profile/profile-id")
	before, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	profile, err := openManagedProfile(root)
	if err != nil {
		t.Fatal("restored IO could not admit original profile", err)
	}
	defer profile.close()
	if profile.id != strings.Repeat("a", 32) {
		t.Fatal("restored IO generated new identity")
	}
	after, err := os.Lstat(path)
	if err != nil || !os.SameFile(before, after) {
		t.Fatal("restored IO replaced original head", err)
	}
}
