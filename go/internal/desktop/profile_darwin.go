//go:build darwin && !ios

package desktop

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
)

type macAppBundle struct{ directory, executable, identifier, info string }

// WebKit uses the main application bundle identifier for its persistent default
// store on macOS13, where WKWebsiteDataStore(forIdentifier:) is unavailable.
// A stable profile-owned bundle keeps app/launcher stores distinct across updates.
// https://github.com/WebKit/WebKit/blob/main/Source/WebKit/UIProcess/WebsiteData/WebsiteDataStoreCocoa.mm
func prepareMacAppBundle(profile, executable string) (macAppBundle, error) {
	var result macAppBundle
	if !filepath.IsAbs(profile) || !filepath.IsAbs(executable) {
		return result, errors.New("native macOS paths must be absolute")
	}
	if err := macPrivateDirectory(profile); err != nil {
		return result, err
	}
	canonical, err := filepath.EvalSymlinks(profile)
	if err != nil {
		return result, errors.New("cannot resolve native macOS profile")
	}
	digest := sha256.Sum256([]byte(canonical))
	result.identifier = "com.deepseekinfra.profile.p" + hex.EncodeToString(digest[:])
	result.directory = filepath.Join(canonical, "native-app", "DeepSeek Infra.app")
	contents := filepath.Join(result.directory, "Contents")
	bin := filepath.Join(contents, "MacOS")
	for _, path := range []string{filepath.Dir(result.directory), result.directory, contents, bin} {
		if err := macPrivateDirectory(path); err != nil {
			return macAppBundle{}, err
		}
	}
	result.executable = filepath.Join(bin, "deepseek-desktop")
	result.info = filepath.Join(contents, "Info.plist")
	for _, path := range []string{result.executable, result.info} {
		if stat, err := os.Lstat(path); err == nil {
			if !stat.Mode().IsRegular() {
				return macAppBundle{}, errors.New("native macOS bundle member is not a regular file")
			}
		} else if !errors.Is(err, os.ErrNotExist) {
			return macAppBundle{}, errors.New("cannot inspect native macOS bundle")
		}
	}
	// Public bundle/ATS keys only; no private WebKit KVC or global ATS bypass.
	metadata := []byte(fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>%s</string>
<key>CFBundleExecutable</key><string>deepseek-desktop</string>
<key>CFBundleName</key><string>DeepSeek Infra</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSPrincipalClass</key><string>NSApplication</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
<key>NSCameraUsageDescription</key><string>Use the camera when you request media capture.</string>
<key>NSMicrophoneUsageDescription</key><string>Use the microphone when you request audio capture.</string>
</dict></plist>
`, result.identifier))
	var previous []byte
	if file, openErr := os.Open(result.info); openErr == nil {
		previous, err = io.ReadAll(io.LimitReader(file, 64*1024+1))
		closeErr := file.Close()
		if err != nil || closeErr != nil || len(previous) > 64*1024 {
			return macAppBundle{}, errors.New("invalid native macOS bundle metadata")
		}
	} else if !errors.Is(openErr, os.ErrNotExist) {
		return macAppBundle{}, errors.New("cannot read native macOS bundle metadata")
	}
	if !bytes.Equal(previous, metadata) {
		if err := macAtomicFile(result.info, bytes.NewReader(metadata), 0o600); err != nil {
			return macAppBundle{}, err
		}
	}
	if filepath.Clean(executable) != result.executable {
		source, err := os.Open(executable)
		if err != nil {
			return macAppBundle{}, errors.New("cannot open native macOS executable")
		}
		defer source.Close()
		stat, err := source.Stat()
		if err != nil || !stat.Mode().IsRegular() || stat.Size() <= 0 || stat.Size() > 256*1024*1024 {
			return macAppBundle{}, errors.New("invalid native macOS executable")
		}
		// Copy into one atomically replaced owned file; never chmod a hard link to
		// the supervisor's source executable or keep a fresh binary per launch.
		if err := macAtomicFile(result.executable, io.LimitReader(source, 256*1024*1024+1), 0o700); err != nil {
			return macAppBundle{}, err
		}
	}
	return result, nil
}

func macPrivateDirectory(path string) error {
	if err := os.MkdirAll(path, 0o700); err != nil {
		return errors.New("cannot create native macOS profile directory")
	}
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("invalid native macOS profile directory")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) {
		return errors.New("native macOS profile has a different owner")
	}
	if err := os.Chmod(path, 0o700); err != nil {
		return errors.New("cannot secure native macOS profile")
	}
	return nil
}

func macAtomicFile(destination string, source io.Reader, mode os.FileMode) error {
	file, err := os.CreateTemp(filepath.Dir(destination), ".native-ui-")
	if err != nil {
		return errors.New("cannot prepare native macOS bundle file")
	}
	name := file.Name()
	defer os.Remove(name)
	var copied int64
	if copied, err = io.Copy(file, io.LimitReader(source, 256*1024*1024+1)); err == nil && copied > 256*1024*1024 {
		err = errors.New("native macOS bundle file exceeds its bound")
	}
	if err == nil {
		err = file.Chmod(mode)
	}
	if err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err != nil || closeErr != nil {
		return errors.New("cannot persist native macOS bundle file")
	}
	if err := os.Rename(name, destination); err != nil {
		return errors.New("cannot publish native macOS bundle file")
	}
	return nil
}

// Re-exec preserves the supervised PID, argv and private environment. It runs
// before Foundation/AppKit initializes NSBundle; no backend is started again.
func ensureMacAppBundle(profile string) (macAppBundle, error) {
	executable, err := os.Executable()
	if err != nil {
		return macAppBundle{}, errors.New("cannot locate native macOS executable")
	}
	result, err := prepareMacAppBundle(profile, executable)
	if err != nil {
		return macAppBundle{}, err
	}
	if filepath.Clean(executable) == result.executable {
		return result, nil
	}
	if err := syscall.Exec(result.executable, os.Args, os.Environ()); err != nil {
		return macAppBundle{}, errors.New("cannot enter native macOS application bundle")
	}
	return macAppBundle{}, errors.New("native macOS application replacement returned")
}
