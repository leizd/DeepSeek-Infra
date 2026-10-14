package launch

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"strings"
	"syscall"
	"testing"
	"time"
)

type rejectingFence struct {
	preparationFailure error
	admission          func(int) error
	closed             bool
}

func (*rejectingFence) attributes() *syscall.SysProcAttr { return nil }
func (f *rejectingFence) prepare(process Process, env []string) (fencedProcess, error) {
	return fencedProcess{path: process.Path, args: []string{process.Path}, env: env}, f.preparationFailure
}
func (f *rejectingFence) admit(pid int) error {
	if f.admission != nil {
		return f.admission(pid)
	}
	return nil
}
func (f *rejectingFence) close() { f.closed = true }

func TestSupervisorCannotRunWithoutMechanicalFence(t *testing.T) {
	denied := errors.New("OS process fence unavailable")
	processes := []Process{{Name: "deepseekd", Path: os.Args[0]}}
	create := func() (supervisorFence, error) { return nil, denied }
	if err := runWithFence(context.Background(), processes, nil, io.Discard, create); !errors.Is(err, denied) || !strings.Contains(err.Error(), "create native process fence") {
		t.Fatal("unavailable fence bypassed native admission", err)
	}
	fence := &rejectingFence{preparationFailure: denied}
	if err := runWithFence(context.Background(), processes, nil, io.Discard, func() (supervisorFence, error) { return fence, nil }); !errors.Is(err, denied) || !fence.closed {
		t.Fatal("failed preparation admitted a process or retained its fence", err)
	}
}

func TestSupervisorAdmissionFailureStopsAlreadyStartedNativeChild(t *testing.T) {
	ready := t.TempDir()
	denied := errors.New("child could not enter the process fence")
	var address string
	fence := &rejectingFence{admission: func(int) error {
		for _, value := range readLaunchListeners(t, ready, 1) {
			address = value
		}
		return denied
	}}
	env := append(os.Environ(), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_LAUNCH_READY_DIR="+ready)
	err := runWithFence(context.Background(), []Process{{Name: "deepseekd", Path: os.Args[0]}}, env, io.Discard, func() (supervisorFence, error) { return fence, nil })
	if !errors.Is(err, denied) || !strings.Contains(err.Error(), "fence deepseekd") || !fence.closed || address == "" {
		t.Fatal("failed process admission was hidden", err)
	}
	connection, err := net.DialTimeout("tcp", address, time.Second)
	if err == nil {
		connection.Close()
		t.Fatal("fence failure left the child listener active")
	}
}
