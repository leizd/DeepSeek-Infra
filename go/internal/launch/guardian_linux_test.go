//go:build linux && !android && !deepseek_android

package launch

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

func init() { launchTestHelper = runGuardianTestHelper }

func runGuardianTestHelper() int {
	switch os.Getenv("DEEPSEEK_GUARDIAN_TEST_MODE") {
	case "root":
		env := append(os.Environ(), "DEEPSEEK_GUARDIAN_TEST_MODE=tree-child")
		if err := Run(context.Background(), []Process{{Name: "deepseekd", Path: os.Args[0]}}, env, os.Stdout); err != nil {
			return 81
		}
		return 0
	case "tree-child":
		cmd := exec.Command(os.Args[0])
		cmd.Env = append(os.Environ(), "DEEPSEEK_GUARDIAN_TEST_MODE=escaped-forker")
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
		if err := cmd.Start(); err != nil {
			return 82
		}
		if err := cmd.Wait(); err != nil {
			return 83
		}
		return runLaunchHelper()
	case "escaped-forker":
		cmd := exec.Command(os.Args[0])
		cmd.Env = append(os.Environ(), "DEEPSEEK_GUARDIAN_TEST_MODE=escaped-leaf")
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
		if err := cmd.Start(); err != nil {
			return 84
		}
		return 0 // The orphaned new-session leaf is adopted by the guardian.
	default:
		return runLaunchHelper()
	}
}

func TestLinuxGuardianRootKillCleansEscapedDescendants(t *testing.T) {
	dir := t.TempDir()
	root := exec.Command(os.Args[0])
	root.Env = append(os.Environ(), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_GUARDIAN_TEST_MODE=root", "DEEPSEEK_LAUNCH_READY_DIR="+dir)
	if err := root.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = root.Process.Kill(); _ = root.Wait() })
	listeners := readLaunchListeners(t, dir, 2)
	// A separate process must survive: cleanup may signal only owned children.
	other := exec.Command(os.Args[0])
	otherDir := t.TempDir()
	other.Env = append(os.Environ(), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_LAUNCH_READY_DIR="+otherDir)
	if err := other.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = other.Process.Kill(); _ = other.Wait() })
	unrelated := readLaunchListeners(t, otherDir, 1)
	if err := root.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = root.Wait()
	deadline := time.Now().Add(5 * time.Second)
	for pid, address := range listeners {
		for {
			connection, err := net.DialTimeout("tcp", address, 100*time.Millisecond)
			if err != nil {
				break
			}
			_ = connection.Close()
			if time.Now().After(deadline) {
				t.Fatalf("owned escaped process %s survived root SIGKILL", pid)
			}
			time.Sleep(20 * time.Millisecond)
		}
		listener, err := net.Listen("tcp", address)
		if err != nil {
			t.Fatalf("owned listener still held: %v", err)
		}
		_ = listener.Close()
	}
	for _, address := range unrelated {
		connection, err := net.DialTimeout("tcp", address, time.Second)
		if err != nil {
			t.Fatalf("guardian killed an unrelated process: %v", err)
		}
		_ = connection.Close()
	}
}

func TestLinuxGuardianCustodyParsesParenthesizedNames(t *testing.T) {
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

func TestLinuxGuardianRejectsInterpreterAndForeignProcessNames(t *testing.T) {
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

func TestLinuxGuardianPrepareKeepsOnlyRootPipeAndOriginalEnvironment(t *testing.T) {
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

// Exercise the real OS guardian core in the instrumented parent, rather than
// counting only a helper's assertions as process coverage. No FD is replaced.
func TestLinuxGuardianCoreSettlesRealChildExitAndCancellation(t *testing.T) {
	var previous int32
	if err := unix.Prctl(unix.PR_GET_CHILD_SUBREAPER, uintptr(unsafe.Pointer(&previous)), 0, 0, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, uintptr(previous), 0, 0, 0) })
	t.Setenv("DEEPSEEK_LAUNCH_HELPER", "1")
	for _, code := range []int{-2, -1, 0, 7} {
		t.Run(strconv.Itoa(code), func(t *testing.T) {
			ready, exit := t.TempDir(), t.TempDir()
			t.Setenv("DEEPSEEK_LAUNCH_READY_DIR", ready)
			t.Setenv("DEEPSEEK_LAUNCH_EXIT_DIR", exit)
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
				code, err := superviseGuardian(Process{Name: "deepseekd", Path: os.Args[0]}, read)
				done <- result{code, err}
			}()
			listeners := readLaunchListeners(t, ready, 1)
			for pid := range listeners {
				if code == -2 {
					number, err := strconv.Atoi(pid)
					if err != nil || !guardianChild(number) {
						t.Fatal("lost custody of natural-exit fixture", err)
					}
					fd, err := unix.PidfdOpen(number, 0)
					if err != nil {
						t.Fatal(err)
					}
					err = unix.PidfdSendSignal(fd, unix.SIGKILL, nil, 0)
					_ = unix.Close(fd)
					if err != nil {
						t.Fatal(err)
					}
				} else if code < 0 {
					_ = write.Close()
				} else if err := os.WriteFile(filepath.Join(exit, pid), []byte(strconv.Itoa(code)), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			select {
			case result := <-done:
				wanted := code
				if code == -2 {
					var childExit *exec.ExitError
					if result.code != 125 || !errors.As(result.err, &childExit) || childExit.ExitCode() != -1 {
						t.Fatal("natural child crash was hidden", result.code, result.err)
					}
					return
				}
				if wanted < 0 {
					wanted = 0
				}
				if result.code != wanted || result.err != nil {
					t.Fatalf("guardian exit result lost: %d %v", result.code, result.err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("real guardian core did not settle")
			}
			for _, address := range listeners {
				listener, err := net.Listen("tcp", address)
				if err != nil {
					t.Fatal("settled guardian retained listener", err)
				}
				_ = listener.Close()
			}
		})
	}
}

func TestLinuxGuardianCoreRejectsMissingLifetimePipeAndBadNativeImage(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "not-a-pipe")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	process := Process{Name: "deepseekd", Path: os.Args[0]}
	if code, err := superviseGuardian(process, file); code != 125 || err == nil {
		t.Fatal("regular file accepted as root lifetime pipe")
	}
	_ = file.Close()
	if code, err := superviseGuardian(process, file); code != 125 || err == nil {
		t.Fatal("closed lifetime file accepted")
	}
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	defer write.Close()
	process.Path = filepath.Join(t.TempDir(), "missing-native")
	if code, err := superviseGuardian(process, read); code != 125 || !errors.Is(err, os.ErrNotExist) {
		t.Fatal("missing image admitted", code, err)
	}
	process.Name = "foreign"
	if code, err := superviseGuardian(process, read); code != 125 || err == nil {
		t.Fatal("foreign image admitted")
	}
}

func TestLinuxGuardianRetainsCustodyThroughTransientCleanupRefusal(t *testing.T) {
	done := make(chan error, 1)
	attempts := 0
	childExit := errors.New("owned child has exited")
	refused := errors.New("sandbox is dropping temporary credentials")
	result := waitGuardianChild(done, func() error {
		attempts++
		if attempts == 3 {
			done <- childExit
			return nil
		}
		return refused
	})
	if attempts != 3 || !errors.Is(result, childExit) {
		t.Fatal("transient signal refusal abandoned the owned child", attempts, result)
	}
}
