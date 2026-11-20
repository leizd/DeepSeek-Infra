//go:build windows

package store

// Intent bytes are flushed with File.Sync before database creation. Windows
// does not expose POSIX directory fsync through os.File; missing intent refuses
// recovery rather than granting fresh authority after a power-loss boundary.
func syncNativeOriginDirectory(string) error { return nil }
