//go:build linux || android

// This instrumentation-only executable probes the kernel locking families used
// by older SQLite VFSes. It never opens SQLite or reads or writes database bytes.
package main

import (
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

func main() {
	if len(os.Args) != 3 {
		os.Exit(64)
	}
	file, err := os.OpenFile(os.Args[2], os.O_RDWR, 0)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer file.Close()
	operation := unix.F_SETLK
	lock := unix.Flock_t{Type: unix.F_RDLCK, Whence: 0, Start: 0x40000002, Len: 510}
	switch os.Args[1] {
	case "ofd":
		operation = unix.F_OFD_SETLK
	case "posix":
	case "hold":
		lock.Type, lock.Start, lock.Len = unix.F_WRLCK, 0, 0
	case "wait":
		fmt.Println("WAITING")
		var command [1]byte
		_, _ = os.Stdin.Read(command[:])
		return
	default:
		os.Exit(64)
	}
	err = unix.FcntlFlock(file.Fd(), operation, &lock)
	if errors.Is(err, unix.EACCES) || errors.Is(err, unix.EAGAIN) {
		fmt.Println("BLOCKED")
		os.Exit(73)
	}
	if os.Args[1] == "ofd" && (errors.Is(err, unix.EINVAL) || errors.Is(err, unix.ENOSYS)) {
		fmt.Printf("UNSUPPORTED errno=%d\n", err)
		os.Exit(78)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if os.Args[1] == "hold" {
		fmt.Println("HELD")
		var command [1]byte
		_, _ = os.Stdin.Read(command[:])
	} else {
		fmt.Println("ACQUIRED")
	}
}
