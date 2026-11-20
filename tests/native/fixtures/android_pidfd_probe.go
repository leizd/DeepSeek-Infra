//go:build linux && amd64

// This host-only executable is built in CI with CGO_ENABLED=0. It never enters
// an APK or release. Kernel TRAP reproduces Android's forbidden pidfd probes.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

func trapPidfds(mode string) error {
	filter := []unix.SockFilter{{Code: unix.BPF_LD | unix.BPF_W | unix.BPF_ABS}}
	for _, entry := range []struct {
		name string
		nr   uint32
	}{{"open", unix.SYS_PIDFD_OPEN}, {"send", unix.SYS_PIDFD_SEND_SIGNAL}} {
		if mode == "both" || mode == entry.name {
			filter = append(filter,
				unix.SockFilter{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: entry.nr, Jf: 1},
				unix.SockFilter{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_TRAP})
		}
	}
	filter = append(filter, unix.SockFilter{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ALLOW})
	program := unix.SockFprog{Len: uint16(len(filter)), Filter: &filter[0]}
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		return err
	}
	result, _, errno := unix.Syscall(unix.SYS_SECCOMP, unix.SECCOMP_SET_MODE_FILTER, unix.SECCOMP_FILTER_FLAG_TSYNC, uintptr(unsafe.Pointer(&program)))
	runtime.KeepAlive(filter)
	runtime.KeepAlive(program)
	if errno != 0 {
		return errno
	}
	if result != 0 {
		return errors.New("could not synchronize the actual TRAP filter")
	}
	return nil
}

func run() error {
	if len(os.Args) < 2 {
		return errors.New("missing probe mode")
	}
	switch os.Args[1] {
	case "echo-child":
		fmt.Printf("child %d %d\n", os.Getpid(), os.Getppid())
		return nil
	case "hold-child":
		fmt.Println("child-ready")
		time.Sleep(time.Minute)
		return nil
	}
	mode := os.Args[1]
	if mode != "open" && mode != "send" && mode != "both" {
		return errors.New("invalid trap mode")
	}
	if err := trapPidfds(mode); err != nil {
		return err
	}
	if len(os.Args) == 3 && os.Args[2] == "forbidden-direct-call" {
		if mode == "send" {
			fd, err := unix.PidfdOpen(os.Getpid(), 0)
			if err != nil {
				return err
			}
			defer unix.Close(fd)
			_ = unix.PidfdSendSignal(fd, 0, nil, 0)
		} else {
			_, _ = unix.PidfdOpen(os.Getpid(), 0)
		}
		return errors.New("forbidden syscall was not fatal")
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	const children = 8
	results := make(chan error, children)
	var peers sync.WaitGroup
	for i := 0; i < children; i++ {
		peers.Add(1)
		go func() {
			defer peers.Done()
			output, err := exec.CommandContext(ctx, executable, "echo-child").CombinedOutput()
			if err != nil {
				results <- err
				return
			}
			var pid, parent int
			if _, err = fmt.Sscanf(strings.TrimSpace(string(output)), "child %d %d", &pid, &parent); err != nil || pid <= 0 || parent != os.Getpid() {
				results <- fmt.Errorf("actual child identity not retained: %s", output)
				return
			}
			results <- nil
		}()
	}
	peers.Wait()
	close(results)
	for err := range results {
		if err != nil {
			return err
		}
	}
	stopCtx, stop := context.WithCancel(ctx)
	defer stop()
	child := exec.CommandContext(stopCtx, executable, "hold-child")
	pipe, err := child.StdoutPipe()
	if err != nil {
		return err
	}
	if err = child.Start(); err != nil {
		return err
	}
	line, readErr := bufio.NewReader(pipe).ReadString('\n')
	stop()
	waitErr := child.Wait()
	if readErr != nil || strings.TrimSpace(line) != "child-ready" || waitErr == nil || ctx.Err() != nil {
		return fmt.Errorf("actual ready child cancellation failed: read=%v wait=%v", readErr, waitErr)
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{
		"status": "PASS", "trap": mode, "actualConcurrentChildren": children,
		"readyChildCancelledAndReaped": true, "broadSignalSuppression": false,
	})
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
