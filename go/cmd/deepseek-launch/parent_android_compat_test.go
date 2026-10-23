//go:build linux && (android || deepseek_android)

package main

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestAndroidParentPipeActualEOFEndsNativeLifetime(t *testing.T) {
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	defer write.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	previous := os.Stdin
	os.Stdin = read
	t.Cleanup(func() { os.Stdin = previous })
	t.Setenv("DEEPSEEK_ANDROID_PARENT_PIPE", "stdin-v1")
	closeParent, err := bindPlatformParent(ctx, cancel)
	if err != nil {
		t.Fatal(err)
	}
	defer closeParent()
	if _, err := write.Write([]byte("parent remains alive")); err != nil {
		t.Fatal(err)
	}
	select {
	case <-ctx.Done():
		t.Fatal("parent data was mistaken for JVM death")
	case <-time.After(50 * time.Millisecond):
	}
	if err := write.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-ctx.Done():
	case <-time.After(2 * time.Second):
		t.Fatal("actual parent EOF did not end native lifetime")
	}
}

func TestAndroidParentLifetimeRefusesNonPipeOrChangedProtocol(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	regular, err := os.CreateTemp(t.TempDir(), "not-a-parent-pipe")
	if err != nil {
		t.Fatal(err)
	}
	defer regular.Close()
	if _, err := observeAndroidParentPipe(ctx, regular, cancel); err == nil {
		t.Fatal("regular file admitted as parent lifetime")
	}
	if err := regular.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := observeAndroidParentPipe(ctx, regular, cancel); err == nil {
		t.Fatal("closed lifetime descriptor admitted")
	}
	t.Setenv("DEEPSEEK_ANDROID_PARENT_PIPE", "future-protocol")
	if _, err := bindPlatformParent(ctx, cancel); err == nil {
		t.Fatal("unknown lifetime protocol admitted")
	}
	if ctx.Err() != nil {
		t.Fatal("refused admission cancelled an unrelated context")
	}
}

func TestAndroidParentLifetimeAllowsCLIAndStopsWithoutCooperatingParent(t *testing.T) {
	t.Setenv("DEEPSEEK_ANDROID_PARENT_PIPE", "")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cleanup, err := bindPlatformParent(ctx, cancel)
	if err != nil {
		t.Fatal(err)
	}
	cleanup()
	if ctx.Err() != nil {
		t.Fatal("CLI without JVM was cancelled")
	}
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	defer write.Close()
	cleanup, err = observeAndroidParentPipe(ctx, read, cancel)
	if err != nil {
		t.Fatal(err)
	}
	closed := make(chan struct{})
	cancel()
	go func() { cleanup(); close(closed) }()
	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("cleanup waited for a live parent's write end")
	}
}

func TestAndroidParentLifetimeDescriptorExhaustionRefusesAdmission(t *testing.T) {
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer read.Close()
	defer write.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var original unix.Rlimit
	if err := unix.Getrlimit(unix.RLIMIT_NOFILE, &original); err != nil {
		t.Fatal(err)
	}
	limited := original
	limited.Cur = 0
	if err := unix.Setrlimit(unix.RLIMIT_NOFILE, &limited); err != nil {
		t.Fatal(err)
	}
	cleanup, admissionErr := observeAndroidParentPipe(ctx, read, cancel)
	restoreErr := unix.Setrlimit(unix.RLIMIT_NOFILE, &original)
	if cleanup != nil {
		cleanup()
	}
	if restoreErr != nil {
		t.Fatal(restoreErr)
	}
	if !errors.Is(admissionErr, unix.EMFILE) {
		t.Fatalf("descriptor exhaustion admitted parent lifetime: %v", admissionErr)
	}
	if ctx.Err() != nil {
		t.Fatal("failed pipe setup changed an unrelated context")
	}
}
