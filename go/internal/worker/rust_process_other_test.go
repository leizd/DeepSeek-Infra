//go:build integration && !windows

package worker

import "os/exec"

func hideTestWorker(_ *exec.Cmd) {}
