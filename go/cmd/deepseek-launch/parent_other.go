//go:build !linux || (!android && !deepseek_android)

package main

import "context"

func bindPlatformParent(context.Context, context.CancelFunc) (func(), error) {
	return func() {}, nil
}
