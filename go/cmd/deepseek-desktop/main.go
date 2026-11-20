//go:build !darwin || ios

package main

import (
	"fmt"
	"os"
	"runtime"
)

// Platform UI frameworks require the initial thread; headless supervision stays independent.
func init() { runtime.LockOSThread() }

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
