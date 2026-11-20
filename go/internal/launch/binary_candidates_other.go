//go:build !linux || (!android && !deepseek_android)

package launch

import "runtime"

func platformBinaryCandidates(name string) []string {
	if runtime.GOOS == "windows" {
		return []string{name + ".exe", name}
	}
	return []string{name}
}
