//go:build linux && (android || deepseek_android) && !sqlite3_flock

package sqlitedb

import (
	"errors"
	"net/url"
)

type NativeControlGuard struct{}

func PrepareControlFile(path string, native, create bool) (NativeControlGuard, error) {
	if native {
		return NativeControlGuard{}, errors.New("Android native control requires the sqlite3_flock build tag")
	}
	return NativeControlGuard{}, createControlFile(path, create)
}

func (NativeControlGuard) Close() error    { return nil }
func configureAndroidVFS(url.Values) error { return nil }
