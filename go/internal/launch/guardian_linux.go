//go:build linux && !android && !deepseek_android

package launch

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
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
			fmt.Fprintln(os.Stderr, "native process guardian:", err)
		}
		os.Exit(code)
	}
}

// The native launcher re-executes itself, not a shell or external supervisor.
// A subreaper also owns grandchildren that escape their original session.
func runGuardian(process Process) (int, error) {
	if !guardianProcessAllowed(process) {
		return 125, errors.New("invalid native guardian process")
	}
	pipe := os.NewFile(3, "native-root-lifetime")
	defer pipe.Close()
	return superviseGuardian(process, pipe)
}

func superviseGuardian(process Process, pipe *os.File) (int, error) {
	if !guardianProcessAllowed(process) {
		return 125, errors.New("invalid native guardian process")
	}
	info, err := pipe.Stat()
	if err != nil || info.Mode()&os.ModeNamedPipe == 0 {
		return 125, errors.New("native root lifetime pipe missing")
	}
	unix.CloseOnExec(int(pipe.Fd()))
	// Probe the stable process-handle API before admitting any child. Do not
	// degrade to PID-only signals on kernels that cannot provide it.
	fd, err := unix.PidfdOpen(os.Getpid(), 0)
	if err != nil {
		return 125, fmt.Errorf("native pidfd support: %w", err)
	}
	err = unix.PidfdSendSignal(fd, 0, nil, 0)
	_ = unix.Close(fd)
	if err != nil {
		return 125, fmt.Errorf("native pidfd signal support: %w", err)
	}
	if _, err := os.ReadDir("/proc"); err != nil {
		return 125, fmt.Errorf("native process custody: %w", err)
	}
	if err := unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0); err != nil {
		return 125, fmt.Errorf("native subreaper: %w", err)
	}
	ended := make(chan struct{})
	go func() {
		var buffer [1]byte
		_, _ = pipe.Read(buffer[:])
		close(ended)
	}()
	cmd := exec.Command(process.Path)
	cmd.Env = os.Environ()
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return 125, err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	var exit error
	cancelled := false
	select {
	case <-ended:
		cancelled = true
		exit = waitGuardianChild(done, killOwnedChildren)
	case exit = <-done:
	}
	// cmd.Wait has reaped the original child. Reap adopted children until the
	// kernel confirms there are none; never abandon a live child on a timer.
	for {
		// A temporarily privileged sandbox helper can deny a signal while it
		// drops credentials. Retain subreaper custody instead of orphaning it.
		_ = killOwnedChildren()
		var status unix.WaitStatus
		_, err := unix.Wait4(-1, &status, unix.WNOHANG, nil)
		if errors.Is(err, unix.ECHILD) {
			break
		}
		if err != nil && !errors.Is(err, unix.EINTR) {
			return 125, err
		}
		time.Sleep(10 * time.Millisecond)
	}
	if cancelled || exit == nil {
		return 0, nil
	}
	var failure *exec.ExitError
	if errors.As(exit, &failure) && failure.ExitCode() >= 0 {
		return failure.ExitCode(), nil
	}
	return 125, exit
}

func waitGuardianChild(done <-chan error, kill func() error) error {
	reported := false
	for {
		if err := kill(); err != nil && !reported {
			fmt.Fprintln(os.Stderr, "native process cleanup delayed:", err)
			reported = true
		}
		select {
		case err := <-done:
			return err
		case <-time.After(10 * time.Millisecond):
		}
	}
}

// /proc only selects this guardian's own children. After opening each pidfd,
// recheck custody; PID recycling can never target a different process.
func killOwnedChildren() error {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return err
	}
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || !guardianChild(pid) {
			continue
		}
		fd, err := unix.PidfdOpen(pid, 0)
		if errors.Is(err, unix.ESRCH) {
			continue
		}
		if err != nil {
			return err
		}
		if guardianChild(pid) {
			err = unix.PidfdSendSignal(fd, unix.SIGKILL, nil, 0)
		}
		_ = unix.Close(fd)
		if err != nil && !errors.Is(err, unix.ESRCH) {
			return err
		}
	}
	return nil
}

func guardianChild(pid int) bool {
	raw, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "stat"))
	if err != nil {
		return false
	}
	return guardianStatChild(raw, os.Getpid())
}

func guardianStatChild(raw []byte, parent int) bool {
	// comm is parenthesized and can itself contain spaces or ')'.
	end := strings.LastIndexByte(string(raw), ')')
	if end < 0 {
		return false
	}
	fields := strings.Fields(string(raw[end+1:]))
	return len(fields) >= 2 && fields[1] == strconv.Itoa(parent)
}
