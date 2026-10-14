//go:build linux && !android && !deepseek_android

package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"reflect"
	"strconv"
	"syscall"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
	"golang.org/x/sys/unix"
)

const promptFixtureKey = "sk-owned-pty-fixture"

func TestMain(m *testing.M) {
	if ready := os.Getenv("DEEPSEEK_TEST_NATIVE_STARTUP_READY"); ready != "" {
		if err := os.WriteFile(filepath.Join(ready, "ready"), []byte("ready"), 0600); err != nil {
			os.Exit(91)
		}
		for {
			time.Sleep(time.Second)
		}
	}
	os.Exit(m.Run())
}

func TestMobileStartupCancellationSettlesActualOwnedProcessWithoutStartupError(t *testing.T) {
	ready := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	plan := launch.Plan{GatewayURL: "http://127.0.0.1:1/", Processes: []launch.Process{{Name: "deepseekd", Path: os.Args[0]}}, Env: []string{"DEEPSEEK_TEST_NATIVE_STARTUP_READY=" + ready}}
	done := make(chan error, 1)
	go func() { done <- runMobile(ctx, plan, launch.Options{Mode: "mobile", NoOpen: true}) }()
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, err := os.Stat(filepath.Join(ready, "ready")); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("owned native startup helper did not start")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal("user cancellation was converted into a startup failure", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("mobile cancellation left its owned process or readiness waiter alive")
	}
}

func TestConsolePTYReaderHelper(t *testing.T) {
	mode := os.Getenv("DEEPSEEK_TEST_SECRET_PTY")
	if mode == "" {
		return
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM)
	defer stop()
	value, err := readConsoleSecret(ctx)
	if mode == "enter" {
		if err != nil || string(value) != promptFixtureKey {
			t.Fatal("masked native input did not return the entered fixture")
		}
	} else if !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled native input did not settle")
	}
	fmt.Fprintln(os.Stderr, "native-secret-result-ok")
}

func TestConsoleActualPTYMasksInputAndRestoresTerminalOnEnterAndCancellation(t *testing.T) {
	for _, mode := range []string{"enter", "ctrl-c", "sigterm"} {
		t.Run(mode, func(t *testing.T) {
			master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|unix.O_NOCTTY|unix.O_NONBLOCK, 0)
			if err != nil {
				t.Fatal(err)
			}
			defer master.Close()
			fd := int(master.Fd())
			if err := unix.IoctlSetPointerInt(fd, unix.TIOCSPTLCK, 0); err != nil {
				t.Fatal(err)
			}
			number, err := unix.IoctlGetInt(fd, unix.TIOCGPTN)
			if err != nil {
				t.Fatal(err)
			}
			slave, err := os.OpenFile("/dev/pts/"+strconv.Itoa(number), os.O_RDWR|unix.O_NOCTTY, 0)
			if err != nil {
				t.Fatal(err)
			}
			defer slave.Close()
			before, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestConsolePTYReaderHelper$")
			cmd.Env = append(os.Environ(), "DEEPSEEK_TEST_SECRET_PTY="+mode)
			cmd.Stdin, cmd.Stdout, cmd.Stderr = slave, slave, slave
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}
			defer cmd.Process.Kill()
			var output bytes.Buffer
			readUntil := func(marker string) {
				if err := master.SetReadDeadline(time.Now().Add(3 * time.Second)); err != nil {
					t.Fatal(err)
				}
				for !bytes.Contains(output.Bytes(), []byte(marker)) {
					var buffer [512]byte
					n, err := master.Read(buffer[:])
					output.Write(buffer[:n])
					if err != nil {
						t.Fatal("native prompt did not reach the expected state", err)
					}
				}
			}
			readUntil("settings): ")
			masked, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
			if err != nil || masked.Lflag&unix.ECHO != 0 {
				t.Fatal("real terminal still echoes secrets", err)
			}
			switch mode {
			case "enter":
				_, err = master.Write([]byte(promptFixtureKey + "\r"))
			case "ctrl-c":
				_, err = master.Write([]byte{3})
			case "sigterm":
				err = cmd.Process.Signal(syscall.SIGTERM)
			}
			if err != nil {
				t.Fatal(err)
			}
			readUntil("native-secret-result-ok")
			if err := cmd.Wait(); err != nil {
				t.Fatal("native prompt helper failed", err)
			}
			after, err := unix.IoctlGetTermios(int(slave.Fd()), unix.TCGETS)
			if err != nil || !reflect.DeepEqual(before, after) {
				t.Fatal("native prompt did not restore the original terminal state", err)
			}
			if bytes.Contains(output.Bytes(), []byte(promptFixtureKey)) {
				t.Fatal("native prompt echoed its credential")
			}
		})
	}
}
