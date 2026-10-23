//go:build !linux && (android || deepseek_android)

package sqlitedb

import "net/url"

func configureAndroidVFS(url.Values) error { return nil }
