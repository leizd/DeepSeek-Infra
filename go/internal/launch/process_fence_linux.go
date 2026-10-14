//go:build linux && !android && !deepseek_android

package launch

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
)

const guardianArgument = "--deepseek-internal-guardian-v1"

// Each guardian owns a read end; only this launcher owns its write end.
// EOF remains observable after SIGKILL, without a cooperative signal handler.
type processFence struct{ pipes *[]*os.File }

func newProcessFence() (processFence, error) {
	pipes := []*os.File{}
	return processFence{pipes: &pipes}, nil
}
func (processFence) attributes() *syscall.SysProcAttr { return nil }
func (processFence) admit(int) error                  { return nil }
func (f processFence) close() {
	for _, pipe := range *f.pipes {
		_ = pipe.Close()
	}
}

func guardianProcessAllowed(process Process) bool {
	switch process.Name {
	case "deepseekd", "deepseek-worker", "deepseek-gateway", "deepseek-desktop":
		return filepath.IsAbs(process.Path) && !legacyCommand(process.Path)
	default:
		return false
	}
}

func (f processFence) prepare(process Process, env []string) (fencedProcess, error) {
	if !guardianProcessAllowed(process) {
		return fencedProcess{}, errors.New("invalid native guardian process")
	}
	info, err := os.Stat(process.Path)
	if err != nil {
		return fencedProcess{}, err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		return fencedProcess{}, errors.New("native guardian target is not executable")
	}
	self, err := os.Executable()
	if err != nil {
		return fencedProcess{}, err
	}
	read, write, err := os.Pipe()
	if err != nil {
		return fencedProcess{}, err
	}
	*f.pipes = append(*f.pipes, read, write)
	return fencedProcess{
		path: self, args: []string{self, guardianArgument, process.Name, process.Path},
		env: env, files: []*os.File{read},
		cancel: func() error { return write.Close() },
	}, nil
}
