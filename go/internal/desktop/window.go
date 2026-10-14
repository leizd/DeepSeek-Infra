package desktop

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"

	entrypkg "github.com/leizd/DeepSeek-Infra/go/internal/desktop/entry"
)

const (
	Title     = "DeepSeek Infra"
	Width     = 1180
	Height    = 820
	MinWidth  = 760
	MinHeight = 520
)

type window interface {
	Navigate(string)
	Dispatch(func())
	Destroy()
	Run() error
}

// Show runs the platform message loop on one OS thread. The app profile is
// persistent and no JS-to-host business interface is installed.
func Show(ctx context.Context, entry, profile string) error {
	return show(ctx, entry, profile, newPlatformWindow)
}

func show(ctx context.Context, entry, profile string, create func(string) (window, error)) error {
	return showWithGuard(ctx, entry, profile, create, nil)
}

func showWithGuard(ctx context.Context, entry, profile string, create func(string) (window, error), closeGuard func(func() bool) bool) error {
	if _, err := entrypkg.EntryURL(entry, ""); err != nil {
		return err
	}
	if !filepath.IsAbs(profile) {
		return errors.New("desktop profile must be absolute")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := os.MkdirAll(profile, 0o700); err != nil {
		return errors.New("cannot create desktop profile")
	}
	info, err := os.Lstat(profile)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("invalid desktop profile")
	}
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	view, err := create(profile)
	if err != nil {
		return err
	}
	defer view.Destroy()
	if closeGuard != nil {
		guarded, ok := view.(interface {
			SetCloseGuard(func() bool)
			ConfirmStop() bool
		})
		if !ok {
			return errors.New("launcher close confirmation is unavailable on this platform")
		}
		guarded.SetCloseGuard(func() bool { return closeGuard(guarded.ConfirmStop) })
	}
	view.Navigate(entry)
	done := make(chan struct{})
	defer close(done)
	go func() {
		select {
		case <-ctx.Done():
			view.Dispatch(view.Destroy)
		case <-done:
		}
	}()
	return view.Run()
}
