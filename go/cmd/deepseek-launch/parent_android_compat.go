//go:build linux && (android || deepseek_android)

package main

import (
	"context"
	"errors"
	"io"
	"os"
	"sync"

	"golang.org/x/sys/unix"
)

// Android's JVM keeps the write end of this pipe. Its actual death closes the
// final writer even after SIGKILL. No parent PID is polled or signalled, and no
// thread-scoped PDEATHSIG is used. Child custody remains in the Go supervisor.
func bindPlatformParent(ctx context.Context, cancel context.CancelFunc) (func(), error) {
	switch os.Getenv("DEEPSEEK_ANDROID_PARENT_PIPE") {
	case "":
		return func() {}, nil
	case "stdin-v1":
		return observeAndroidParentPipe(ctx, os.Stdin, cancel)
	default:
		return nil, errors.New("unsupported Android parent lifetime protocol")
	}
}

func observeAndroidParentPipe(ctx context.Context, source *os.File, cancel context.CancelFunc) (func(), error) {
	info, err := source.Stat()
	if err != nil {
		return nil, err
	}
	if info.Mode()&os.ModeNamedPipe == 0 {
		return nil, errors.New("Android parent lifetime requires a pipe")
	}
	fd, err := unix.FcntlInt(source.Fd(), unix.F_DUPFD_CLOEXEC, 3)
	if err != nil {
		return nil, err
	}
	if err := unix.SetNonblock(fd, true); err != nil {
		_ = unix.Close(fd)
		return nil, err
	}
	pipe := os.NewFile(uintptr(fd), "android-parent-lifetime")
	var once sync.Once
	closePipe := func() { once.Do(func() { _ = pipe.Close() }) }
	ended := make(chan struct{})
	go func() {
		defer close(ended)
		_, _ = io.Copy(io.Discard, pipe)
		cancel()
		closePipe()
	}()
	go func() {
		select {
		case <-ctx.Done():
			closePipe()
		case <-ended:
		}
	}()
	return closePipe, nil
}
