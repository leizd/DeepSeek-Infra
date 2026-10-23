//go:build linux && (android || deepseek_android)

package launch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

func init() { launchTestHelper = runAndroidGuardianTestHelper }

func androidGuardianHelperEnv(mode string) []string {
	env := []string{}
	for _, item := range os.Environ() {
		if !strings.HasPrefix(item, "DEEPSEEK_ANDROID_GUARDIAN_MODE=") {
			env = append(env, item)
		}
	}
	return append(env, "DEEPSEEK_ANDROID_GUARDIAN_MODE="+mode)
}

func runAndroidGuardianTestHelper() int {
	switch os.Getenv("DEEPSEEK_ANDROID_GUARDIAN_MODE") {
	case "root":
		if err := androidGuardianDenyPidfds(); err != nil {
			fmt.Fprintln(os.Stderr, "test pidfd denial:", err)
			return 91
		}
		signal.Ignore(syscall.SIGCHLD) // The exec'd guardian must restore waitable children.
		if err := Run(context.Background(), []Process{{Name: "deepseekd", Path: os.Args[0]}}, androidGuardianHelperEnv("tree"), io.Discard); err != nil {
			return 92
		}
		return 0
	case "tree", "forker":
		mode := "forker"
		if os.Getenv("DEEPSEEK_ANDROID_GUARDIAN_MODE") == "forker" {
			mode = "leaf"
		}
		cmd := exec.Command(os.Args[0])
		cmd.Env = androidGuardianHelperEnv(mode)
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
		if err := cmd.Start(); err != nil {
			return 93
		}
		if mode == "leaf" {
			return 0 // The new-session leaf must be adopted by the guardian.
		}
		if err := cmd.Wait(); err != nil {
			return 94
		}
		return androidGuardianTestListener()
	case "leaf":
		return androidGuardianTestListener()
	default:
		return runLaunchHelper()
	}
}

func androidGuardianTestListener() int {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 95
	}
	defer listener.Close()
	file := filepath.Join(os.Getenv("DEEPSEEK_LAUNCH_READY_DIR"), strconv.Itoa(os.Getpid()))
	if err := os.WriteFile(file, []byte(listener.Addr().String()), 0o600); err != nil {
		return 96
	}
	go func() {
		for {
			connection, err := listener.Accept()
			if err != nil {
				return
			}
			_ = connection.Close()
		}
	}()
	for {
		if _, err := os.Stat(os.Getenv("DEEPSEEK_ANDROID_GUARDIAN_STOP")); err == nil {
			return 0
		}
		if body, err := os.ReadFile(os.Getenv("DEEPSEEK_ANDROID_GUARDIAN_EXIT")); err == nil {
			if string(body) == "kill" {
				_ = unix.Kill(os.Getpid(), unix.SIGKILL)
				return 97
			}
			code, err := strconv.Atoi(string(body))
			if err != nil {
				return 98
			}
			return code
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// The cloud host has a modern kernel. Make both pidfd operations unavailable
// to every launcher/guardian/worker thread and to all subsequently exec'd images.
func androidGuardianDenyPidfds() error {
	filter := []unix.SockFilter{
		{Code: unix.BPF_LD | unix.BPF_W | unix.BPF_ABS},
		{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: unix.SYS_PIDFD_OPEN, Jf: 1},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ERRNO | uint32(unix.ENOSYS)},
		{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: unix.SYS_PIDFD_SEND_SIGNAL, Jf: 1},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ERRNO | uint32(unix.ENOSYS)},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ALLOW},
	}
	program := unix.SockFprog{Len: uint16(len(filter)), Filter: &filter[0]}
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		return err
	}
	synced, _, errno := unix.Syscall(unix.SYS_SECCOMP, unix.SECCOMP_SET_MODE_FILTER, unix.SECCOMP_FILTER_FLAG_TSYNC, uintptr(unsafe.Pointer(&program)))
	if errno != 0 {
		return errno
	}
	if synced != 0 {
		return fmt.Errorf("seccomp synchronization failed at thread %d", synced)
	}
	if fd, err := unix.PidfdOpen(os.Getpid(), 0); !errors.Is(err, unix.ENOSYS) {
		if fd >= 0 {
			_ = unix.Close(fd)
		}
		return fmt.Errorf("pidfd denial was not installed: %v", err)
	}
	return nil
}

func TestAndroidGuardianRootKillCleansEscapedDescendantsWithoutPidfds(t *testing.T) {
	dir := t.TempDir()
	stop := filepath.Join(t.TempDir(), "stop")
	var listeners map[string]string
	t.Cleanup(func() {
		_ = os.WriteFile(stop, []byte("stop"), 0o600)
		deadline := time.Now().Add(3 * time.Second)
		for _, address := range listeners {
			for {
				connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
				if err != nil {
					break
				}
				_ = connection.Close()
				if time.Now().After(deadline) {
					t.Error("cooperative fixture cleanup did not stop listener")
					break
				}
				time.Sleep(10 * time.Millisecond)
			}
		}
	})
	root := exec.Command(os.Args[0])
	root.Env = append(androidGuardianHelperEnv("root"), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_LAUNCH_READY_DIR="+dir, "DEEPSEEK_ANDROID_GUARDIAN_STOP="+stop)
	root.Stderr = os.Stderr
	if err := root.Start(); err != nil {
		t.Fatal(err)
	}
	waited := false
	t.Cleanup(func() {
		if !waited {
			_ = root.Process.Kill()
			_ = root.Wait()
		}
	})
	listeners = readLaunchListeners(t, dir, 2)
	unrelatedDir := t.TempDir()
	unrelated := exec.Command(os.Args[0])
	unrelated.Env = append(androidGuardianHelperEnv("leaf"), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_LAUNCH_READY_DIR="+unrelatedDir)
	if err := unrelated.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = unrelated.Process.Kill(); _ = unrelated.Wait() })
	other := readLaunchListeners(t, unrelatedDir, 1)
	if err := root.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = root.Wait()
	waited = true
	deadline := time.Now().Add(5 * time.Second)
	for pid, address := range listeners {
		for {
			connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
			if err != nil {
				break
			}
			_ = connection.Close()
			if time.Now().After(deadline) {
				t.Fatalf("owned escaped process %s survived launcher SIGKILL without pidfds", pid)
			}
			time.Sleep(20 * time.Millisecond)
		}
		listener, err := net.Listen("tcp", address)
		if err != nil {
			t.Fatal("owned listener still held:", err)
		}
		_ = listener.Close()
	}
	for _, address := range other {
		connection, err := net.DialTimeout("tcp", address, time.Second)
		if err != nil {
			t.Fatal("guardian killed an unrelated process:", err)
		}
		_ = connection.Close()
	}
}

func TestAndroidGuardianCoreSettlesRealExitAndCancellation(t *testing.T) {
	var previous int32
	if err := unix.Prctl(unix.PR_GET_CHILD_SUBREAPER, uintptr(unsafe.Pointer(&previous)), 0, 0, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, uintptr(previous), 0, 0, 0) })
	t.Setenv("DEEPSEEK_LAUNCH_HELPER", "1")
	t.Setenv("DEEPSEEK_ANDROID_GUARDIAN_MODE", "leaf")
	for _, code := range []string{"0", "7", "kill", "cancel"} {
		t.Run(code, func(t *testing.T) {
			ready, exit := t.TempDir(), filepath.Join(t.TempDir(), "exit")
			t.Setenv("DEEPSEEK_LAUNCH_READY_DIR", ready)
			t.Setenv("DEEPSEEK_ANDROID_GUARDIAN_EXIT", exit)
			read, write, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			defer read.Close()
			defer write.Close()
			type result struct {
				code int
				err  error
			}
			done := make(chan result, 1)
			go func() {
				value, err := superviseGuardian(Process{Name: "deepseekd", Path: os.Args[0]}, read)
				done <- result{value, err}
			}()
			listeners := readLaunchListeners(t, ready, 1)
			if code == "cancel" {
				_ = write.Close()
			} else if err := os.WriteFile(exit, []byte(code), 0o600); err != nil {
				t.Fatal(err)
			}
			select {
			case value := <-done:
				if code == "kill" {
					if value.code != 125 || value.err == nil {
						t.Fatal("natural child crash hidden:", value)
					}
				} else {
					wanted, _ := strconv.Atoi(code)
					if value.code != wanted || value.err != nil {
						t.Fatal("child exit/cancellation lost:", value)
					}
				}
			case <-time.After(5 * time.Second):
				t.Fatal("guardian did not settle actual child")
			}
			for _, address := range listeners {
				listener, err := net.Listen("tcp", address)
				if err != nil {
					t.Fatal("settled child retained listener:", err)
				}
				_ = listener.Close()
			}
		})
	}
}

func TestAndroidGuardianRefusesMissingPipeAndForeignImage(t *testing.T) {
	process := Process{Name: "deepseekd", Path: os.Args[0]}
	file, err := os.CreateTemp(t.TempDir(), "not-a-pipe")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if code, err := superviseGuardian(process, file); code != 125 || err == nil {
		t.Fatal("regular file admitted")
	}
	_ = file.Close()
	if code, err := superviseGuardian(process, file); code != 125 || err == nil {
		t.Fatal("closed file admitted")
	}
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	defer write.Close()
	process.Name = "foreign"
	if code, err := superviseGuardian(process, read); code != 125 || err == nil {
		t.Fatal("foreign process admitted")
	}
	var previous int32
	if err := unix.Prctl(unix.PR_GET_CHILD_SUBREAPER, uintptr(unsafe.Pointer(&previous)), 0, 0, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, uintptr(previous), 0, 0, 0) })
	process = Process{Name: "deepseekd", Path: filepath.Join(t.TempDir(), "missing-native")}
	if code, err := superviseGuardian(process, read); code != 125 || !errors.Is(err, os.ErrNotExist) {
		t.Fatal("missing image admitted a child or hid the startup error", code, err)
	}
}

func TestAndroidGuardianCustodyParsesParenthesizedNames(t *testing.T) {
	for _, raw := range []string{"12 (worker ( ) )) S 42 1", "12 (worker) Z 42 1"} {
		if !guardianStatChild([]byte(raw), 42) || guardianStatChild([]byte(raw), 4) {
			t.Fatalf("custody parsed incorrectly: %s", raw)
		}
	}
	for _, raw := range []string{"", "12 worker S 42", "12 (worker)", "12 (worker) S"} {
		if guardianStatChild([]byte(raw), 42) {
			t.Fatalf("malformed custody accepted: %s", raw)
		}
	}
	if guardianChild(os.Getpid()) || guardianChild(-1) {
		t.Fatal("non-child admitted to guardian custody")
	}
}

func TestAndroidGuardianRejectsInterpreterAndForeignProcessNames(t *testing.T) {
	for _, process := range []Process{
		{Name: "deepseekd", Path: "/usr/bin/python"},
		{Name: "deepseek-worker", Path: "/usr/bin/node"},
		{Name: "shell", Path: "/bin/sh"},
		{Name: "deepseekd", Path: "relative"},
	} {
		if guardianProcessAllowed(process) {
			t.Fatalf("foreign process admitted: %s", process.Name)
		}
		if code, err := runGuardian(process); code != 125 || err == nil {
			t.Fatalf("invalid guardian target: %d %v", code, err)
		}
	}
}

func TestAndroidGuardianPrepareKeepsOnlyRootPipeAndOriginalEnvironment(t *testing.T) {
	fence, err := newProcessFence()
	if err != nil {
		t.Fatal(err)
	}
	defer fence.close()
	env := []string{"ONLY_UI_CONFIGURATION=retained"}
	prepared, err := fence.prepare(Process{Name: "deepseek-desktop", Path: os.Args[0]}, env)
	if err != nil {
		t.Fatal(err)
	}
	if len(prepared.files) != 1 || len(prepared.args) != 4 || prepared.args[1] != guardianArgument || prepared.env[0] != env[0] {
		t.Fatal("guardian changed native entry or environment")
	}
	if err := prepared.cancel(); err != nil {
		t.Fatal(err)
	}
	var buffer [1]byte
	if _, err := prepared.files[0].Read(buffer[:]); !errors.Is(err, io.EOF) {
		t.Fatalf("root lifetime pipe retained by another owner: %v", err)
	}
	if fence.attributes() != nil || fence.admit(1) != nil {
		t.Fatal("guardian unexpectedly changes root process attributes")
	}
	missing := filepath.Join(t.TempDir(), "missing-worker")
	if _, err := fence.prepare(Process{Name: "deepseek-worker", Path: missing}, nil); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("missing native binary admitted: %v", err)
	}
	plain := filepath.Join(t.TempDir(), "native")
	if err := os.WriteFile(plain, []byte(strconv.Itoa(os.Getpid())), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := fence.prepare(Process{Name: "deepseekd", Path: plain}, nil); err == nil {
		t.Fatal("non-executable target admitted")
	}
	if _, err := fence.prepare(Process{Name: "foreign", Path: os.Args[0]}, nil); err == nil {
		t.Fatal("foreign target admitted")
	}
}
