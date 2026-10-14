//go:build !windows && (!linux || android || deepseek_android)

package desktop

import (
	"errors"
)

// The retained Linux/macOS window hosts are an explicit open migration gate.
// Headless native operation continues to use deepseek-launch --server.
func newPlatformWindow(string) (window, error) {
	return nil, errors.New("native desktop window qualification is pending on this platform")
}
