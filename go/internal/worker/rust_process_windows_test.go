//go:build integration && windows

package worker

import (
	"os/exec"
	"syscall"
)

func hideTestWorker(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
}
