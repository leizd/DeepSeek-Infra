//go:build !windows && (!linux || android || deepseek_android)

package desktop

import "errors"

func openPlatformBrowser(string) error {
	return errors.New("default browser handler unavailable")
}
