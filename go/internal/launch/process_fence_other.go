//go:build !windows && (!linux || android || deepseek_android)

package launch

import "syscall"

type processFence struct{}

func newProcessFence() (processFence, error)          { return processFence{}, nil }
func (processFence) attributes() *syscall.SysProcAttr { return nil }
func (processFence) admit(int) error                  { return nil }
func (processFence) close()                           {}

func (processFence) prepare(process Process, env []string) (fencedProcess, error) {
	return fencedProcess{path: process.Path, args: []string{process.Path}, env: env}, nil
}
