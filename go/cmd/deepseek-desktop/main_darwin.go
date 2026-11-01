//go:build darwin && !ios

package main

import (
	"fmt"
	"os"
	"runtime"

	"github.com/leizd/DeepSeek-Infra/go/internal/desktop"
)

func init() { runtime.LockOSThread() }

func main() {
	var err error
	if os.Getenv("DEEPSEEK_DESKTOP_KIND") == "browser" {
		err = desktop.OpenMacBrowserOnInitialThread(os.Getenv("DEEPSEEK_BROWSER_URL"))
	} else {
		err = run()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
