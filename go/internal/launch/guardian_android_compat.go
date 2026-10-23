//go:build linux && (android || deepseek_android)

package launch

import (
	"errors"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

func init() {
	if len(os.Args) == 4 && os.Args[1] == guardianArgument {
		code, err := runGuardian(Process{Name: os.Args[2], Path: os.Args[3]})
		if err != nil {
			fmt.Fprintln(os.Stderr, "native Android process guardian:", err)
		}
		os.Exit(code)
	}
}

func runGuardian(process Process) (int, error) {
	if !guardianProcessAllowed(process) {
		return 125, errors.New("invalid native guardian process")
	}
	pipe := os.NewFile(3, "native-root-lifetime")
	defer pipe.Close()
	return superviseGuardian(process, pipe)
}

// Android's oldest supported kernel has subreapers, but lacks pidfds. This
// dedicated guardian is the sole reaper of its children: a child PID remains
// reserved, including after exit, until this same loop calls Wait4. No other
// goroutine calls Process.Wait or Wait4. Signals target only verified immediate
// children, never a process group or a formerly observed, already-reaped PID.
func superviseGuardian(process Process, pipe *os.File) (int, error) {
	if !guardianProcessAllowed(process) {
		return 125, errors.New("invalid native guardian process")
	}
	info, err := pipe.Stat()
	if err != nil || info.Mode()&os.ModeNamedPipe == 0 {
		return 125, errors.New("native root lifetime pipe missing")
	}
	unix.CloseOnExec(int(pipe.Fd()))
	if _, err := os.ReadDir("/proc"); err != nil {
		return 125, fmt.Errorf("native process custody: %w", err)
	}
	if err := unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0); err != nil {
		return 125, fmt.Errorf("native subreaper: %w", err)
	}
	// Install Go's SIGCHLD handler before starting any child. In particular,
	// inherited SIG_IGN/SA_NOCLDWAIT must not auto-reap and free a child's PID.
	children := make(chan os.Signal, 1)
	signal.Notify(children, syscall.SIGCHLD)
	defer signal.Stop(children)
	ended := make(chan struct{})
	go func() {
		var buffer [1]byte
		_, _ = pipe.Read(buffer[:])
		close(ended)
	}()
	child, err := os.StartProcess(process.Path, []string{process.Path}, &os.ProcAttr{
		Env: os.Environ(), Files: []*os.File{os.Stdin, os.Stdout, os.Stderr},
		Sys: &syscall.SysProcAttr{Setsid: true},
	})
	if err != nil {
		return 125, err
	}
	defer child.Release() // Close Go's handle; only the loop below reaps it.
	stopping, cancelled, rootExited, reported := false, false, false, false
	code := 0
	var rootError error
	for {
		if stopping {
			if err := killOwnedAndroidChildren(); err != nil && !reported {
				fmt.Fprintln(os.Stderr, "native Android process cleanup delayed:", err)
				reported = true
			}
		}
		for {
			var status unix.WaitStatus
			pid, err := unix.Wait4(-1, &status, unix.WNOHANG, nil)
			if errors.Is(err, unix.EINTR) {
				continue
			}
			if errors.Is(err, unix.ECHILD) {
				if !rootExited {
					return 125, errors.New("native child disappeared without a waitable exit")
				}
				if cancelled {
					return 0, nil
				}
				return code, rootError
			}
			if err != nil {
				// Retain custody through an unexpected wait failure. Returning
				// here could abandon a live worker or an adopted descendant.
				stopping = true
				if !reported {
					fmt.Fprintln(os.Stderr, "native Android child wait delayed:", err)
					reported = true
				}
				break
			}
			if pid == 0 {
				break
			}
			if pid == child.Pid {
				rootExited, stopping = true, true
				if status.Exited() {
					code = status.ExitStatus()
				} else {
					code, rootError = 125, fmt.Errorf("native child terminated by signal %d", status.Signal())
				}
			}
		}
		if stopping {
			// Escaped descendants can be adopted while their parents exit.
			// Retry until ECHILD; never abandon custody at a deadline.
			select {
			case <-children:
			case <-time.After(10 * time.Millisecond):
			}
		} else {
			select {
			case <-ended:
				stopping, cancelled = true, true
			case <-children:
			}
		}
	}
}

// Only superviseGuardian may call this, between its own Wait4 calls. A child
// that exits after the custody check remains a zombie with the same reserved
// PID until that loop reaps it. Go's Process.Wait is deliberately not running.
func killOwnedAndroidChildren() error {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return err
	}
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || !guardianChild(pid) {
			continue
		}
		if err := unix.Kill(pid, unix.SIGKILL); err != nil && !errors.Is(err, unix.ESRCH) {
			return err
		}
	}
	return nil
}

func guardianChild(pid int) bool {
	raw, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "stat"))
	return err == nil && guardianStatChild(raw, os.Getpid())
}

func guardianStatChild(raw []byte, parent int) bool {
	end := strings.LastIndexByte(string(raw), ')')
	if end < 0 {
		return false
	}
	fields := strings.Fields(string(raw[end+1:]))
	return len(fields) >= 2 && fields[1] == strconv.Itoa(parent)
}
