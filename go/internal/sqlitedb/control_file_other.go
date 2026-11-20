//go:build !linux || (!android && !deepseek_android)

package sqlitedb

// Other platforms retain their driver's native interoperable lock protocol.
type NativeControlGuard struct{}

func PrepareControlFile(path string, native, create bool) (NativeControlGuard, error) {
	return NativeControlGuard{}, createControlFile(path, create)
}

func (NativeControlGuard) Close() error { return nil }
