//go:build linux && (android || deepseek_android)

package profilevalue

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

// This filter is installed only in an isolated child, across all its threads.
func denyNewPublicationSyscalls() error {
	filter := []unix.SockFilter{
		{Code: unix.BPF_LD | unix.BPF_W | unix.BPF_ABS},
		{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: unix.SYS_LINKAT, Jf: 1},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ERRNO | uint32(unix.EACCES)},
		{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: unix.SYS_RENAMEAT2, Jf: 1},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ERRNO | uint32(unix.ENOSYS)},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ALLOW},
	}
	program := unix.SockFprog{Len: uint16(len(filter)), Filter: &filter[0]}
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		return err
	}
	result, _, errno := unix.Syscall(unix.SYS_SECCOMP, unix.SECCOMP_SET_MODE_FILTER, unix.SECCOMP_FILTER_FLAG_TSYNC, uintptr(unsafe.Pointer(&program)))
	runtime.KeepAlive(program)
	runtime.KeepAlive(filter)
	if errno != 0 {
		return errno
	}
	if result != 0 {
		return fmt.Errorf("seccomp synchronization stopped at thread %d", result)
	}
	return nil
}

func denyProfileValueSync() error {
	filter := []unix.SockFilter{
		{Code: unix.BPF_LD | unix.BPF_W | unix.BPF_ABS},
		{Code: unix.BPF_JMP | unix.BPF_JEQ | unix.BPF_K, K: unix.SYS_FSYNC, Jf: 1},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ERRNO | uint32(unix.EIO)},
		{Code: unix.BPF_RET | unix.BPF_K, K: unix.SECCOMP_RET_ALLOW},
	}
	program := unix.SockFprog{Len: uint16(len(filter)), Filter: &filter[0]}
	if err := unix.Prctl(unix.PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0); err != nil {
		return err
	}
	result, _, errno := unix.Syscall(unix.SYS_SECCOMP, unix.SECCOMP_SET_MODE_FILTER, unix.SECCOMP_FILTER_FLAG_TSYNC, uintptr(unsafe.Pointer(&program)))
	runtime.KeepAlive(program)
	runtime.KeepAlive(filter)
	if errno != 0 {
		return errno
	}
	if result != 0 {
		return fmt.Errorf("sync seccomp synchronization stopped at thread %d", result)
	}
	return nil
}

func TestAndroidProfileValuePeer(t *testing.T) {
	mode := os.Getenv("DEEPSEEK_PROFILE_VALUE_TEST_MODE")
	if mode == "" {
		return
	}
	path := os.Getenv("DEEPSEEK_PROFILE_VALUE_TEST_PATH")
	value := []byte(strings.Repeat("a", 32))
	if mode == "winner-sync-ack" {
		if err := Publish(path, value); err != nil {
			t.Fatal(err)
		}
		err := publishAt(path, value, func(stage string) {
			if stage == "record-directory-synced" {
				if err := denyProfileValueSync(); err != nil {
					t.Fatal(err)
				}
			}
		})
		if !errors.Is(err, unix.EIO) {
			t.Fatal("competing winner bypassed durability confirmation", err)
		}
		return
	}
	if mode == "early-directory-sync" {
		err := publishAt(path, value, func(stage string) {
			if stage == "record-synced" {
				if err := denyProfileValueSync(); err != nil {
					t.Fatal(err)
				}
			}
		})
		if !errors.Is(err, unix.EIO) {
			t.Fatal("actual pre-head directory sync failure missing", err)
		}
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatal("failed directory sync published head", err)
		}
		return
	}
	if mode == "lost-sync-ack" {
		err := publishAt(path, value, func(stage string) {
			if stage == "head-created" {
				if err := denyProfileValueSync(); err != nil {
					t.Fatal(err)
				}
			}
		})
		if !errors.Is(err, unix.EIO) {
			t.Fatal("actual final-sync failure not installed", err)
		}
		before, err := os.Lstat(path)
		if err != nil {
			t.Fatal(err)
		}
		if raw, err := Read(path, 64); err != nil || string(raw) != string(value) {
			t.Fatal("unacknowledged complete head lost", err)
		}
		if err := Publish(path, value); !errors.Is(err, unix.EIO) {
			t.Fatal("visible unacknowledged head bypassed durability barrier", err)
		}
		if err := ConfirmDurable(path, value); !errors.Is(err, unix.EIO) {
			t.Fatal("sync failure was acknowledged during admission", err)
		}
		after, err := os.Lstat(path)
		if err != nil || !os.SameFile(before, after) {
			t.Fatal("sync failure replaced original head", err)
		}
		return
	}
	if mode == "old-kernel" {
		if err := denyNewPublicationSyscalls(); err != nil {
			t.Fatal(err)
		}
		probe := filepath.Join(filepath.Dir(path), "probe")
		if err := os.WriteFile(probe, value, 0o600); err != nil {
			t.Fatal(err)
		}
		defer os.Remove(probe)
		if err := os.Link(probe, probe+".link"); !errors.Is(err, unix.EACCES) {
			t.Fatal("hard links not denied", err)
		}
		if err := unix.Renameat2(unix.AT_FDCWD, probe, unix.AT_FDCWD, probe+".move", unix.RENAME_NOREPLACE); !errors.Is(err, unix.ENOSYS) {
			t.Fatal("renameat2 not unavailable", err)
		}
		if err := Publish(path, value); err != nil {
			t.Fatal("publication still requires unavailable kernel operations", err)
		}
		if raw, err := Read(path, 64); err != nil || string(raw) != string(value) {
			t.Fatal("publication incomplete", err)
		}
		return
	}
	err := publishAt(path, value, func(stage string) {
		if stage == mode {
			_ = unix.Kill(os.Getpid(), unix.SIGKILL)
		}
	})
	t.Fatal("crash checkpoint not reached", mode, err)
}

func publicationChild(t *testing.T, path, mode string, killed bool) {
	t.Helper()
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	args := append([]string{"-test.run=^TestAndroidProfileValuePeer$"}, profileCoverageArguments()...)
	child := exec.CommandContext(ctx, executable, args...)
	child.Env = append(os.Environ(), "DEEPSEEK_PROFILE_VALUE_TEST_MODE="+mode, "DEEPSEEK_PROFILE_VALUE_TEST_PATH="+path)
	output, err := child.CombinedOutput()
	if killed {
		var exit *exec.ExitError
		if !errors.As(err, &exit) || exit.Sys().(syscall.WaitStatus).Signal() != syscall.SIGKILL {
			t.Fatalf("actual SIGKILL absent: %v %s", err, output)
		}
	} else if err != nil {
		t.Fatalf("publication child failed: %v %s", err, output)
	}
}

func TestAndroidProfileValueWorksWithoutLinksOrRenameat2(t *testing.T) {
	path := profilePath(t)
	publicationChild(t, path, "old-kernel", false)
	if raw, err := Read(path, 64); err != nil || string(raw) != strings.Repeat("a", 32) {
		t.Fatal("original value absent", err)
	}
}

func TestAndroidProfileValueLostSyncAcknowledgementMustBeReconfirmed(t *testing.T) {
	for _, mode := range []string{"lost-sync-ack", "winner-sync-ack"} {
		t.Run(mode, func(t *testing.T) {

			path := profilePath(t)
			publicationChild(t, path, mode, false)
			before, err := os.Lstat(path)
			if err != nil {
				t.Fatal(err)
			}
			value := []byte(strings.Repeat("a", 32))
			if err := ConfirmDurable(path, value); err != nil {
				t.Fatal("recovery could not confirm original bytes", err)
			}
			if err := Publish(path, value); err != nil {
				t.Fatal(err)
			}
			after, err := os.Lstat(path)
			if err != nil || !os.SameFile(before, after) {
				t.Fatal("recovery replaced original head", err)
			}
			if raw, err := Read(path, 64); err != nil || string(raw) != string(value) {
				t.Fatal("recovery replaced original value", err)
			}

		})
	}
}

func TestAndroidProfileValueActualKillNeverExposesPartialHead(t *testing.T) {
	for _, phase := range []string{"record-created", "record-written", "record-synced", "record-directory-synced", "head-created", "head-synced"} {
		t.Run(phase, func(t *testing.T) {
			path := profilePath(t)
			publicationChild(t, path, phase, true)
			if strings.HasPrefix(phase, "head-") {
				if raw, err := Read(path, 64); err != nil || string(raw) != strings.Repeat("a", 32) {
					t.Fatal("interrupted head lost complete original bytes", err)
				}
				before, _ := os.Lstat(path)
				if err := Publish(path, []byte(strings.Repeat("a", 32))); err != nil {
					t.Fatal(err)
				}
				after, _ := os.Lstat(path)
				if !os.SameFile(before, after) {
					t.Fatal("recovery replaced acknowledged head")
				}
			} else {
				if _, err := Read(path, 64); !errors.Is(err, os.ErrNotExist) {
					t.Fatal("partial record became authoritative", err)
				}
				if !UnpublishedPool(filepath.Join(filepath.Dir(path), poolName)) {
					t.Fatal("interrupted stage not recognized")
				}
				if err := Publish(path, []byte(strings.Repeat("b", 32))); err != nil {
					t.Fatal("pre-admission restart could not publish", err)
				}
			}
		})
	}
}

func TestAndroidProfileValueRejectsForgedPointersAndUnsafeRecords(t *testing.T) {
	for _, kind := range []string{"absolute", "escape", "cross-scope", "hash", "dangling", "record-symlink", "record-directory", "record-mode", "pool-mode", "pool-symlink", "over-limit"} {
		t.Run(kind, func(t *testing.T) {
			path := profilePath(t)
			if err := Publish(path, []byte(strings.Repeat("a", 32))); err != nil {
				t.Fatal(err)
			}
			target, err := os.Readlink(path)
			if err != nil {
				t.Fatal(err)
			}
			record := filepath.Join(filepath.Dir(path), filepath.FromSlash(target))
			if kind == "record-mode" {
				if err = os.Chmod(record, 0o644); err != nil {
					t.Fatal(err)
				}
			}
			if kind == "pool-mode" {
				if err = os.Chmod(filepath.Dir(record), 0o755); err != nil {
					t.Fatal(err)
				}
			}
			if kind == "hash" || kind == "over-limit" {
				value := "tampered"
				if kind == "over-limit" {
					value = strings.Repeat("x", 4097)
				}
				if err = os.WriteFile(record, []byte(value), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			if kind == "dangling" || strings.HasPrefix(kind, "record-") && kind != "record-mode" {
				if err = os.Remove(record); err != nil {
					t.Fatal(err)
				}
				if kind == "record-symlink" {
					outside := filepath.Join(t.TempDir(), "foreign")
					if err = os.WriteFile(outside, []byte(strings.Repeat("a", 32)), 0o600); err != nil {
						t.Fatal(err)
					}
					if err = os.Symlink(outside, record); err != nil {
						t.Fatal(err)
					}
				}
				if kind == "record-directory" {
					if err = os.Mkdir(record, 0o700); err != nil {
						t.Fatal(err)
					}
				}
			}
			if kind == "pool-symlink" {
				pool := filepath.Dir(record)
				moved := pool + "-foreign"
				if err = os.Rename(pool, moved); err != nil {
					t.Fatal(err)
				}
				if err = os.Symlink(moved, pool); err != nil {
					t.Fatal(err)
				}
			}
			if kind == "absolute" || kind == "escape" || kind == "cross-scope" {
				if err = os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if kind == "absolute" {
					target = record
				}
				if kind == "escape" {
					target = "../native-profile/" + target
				}
				if kind == "cross-scope" {
					target = strings.Replace(target, "profile-id.", "signer-public-pin.", 1)
				}
				if err = os.Symlink(target, path); err != nil {
					t.Fatal(err)
				}
			}
			before, _ := os.Lstat(path)
			if _, err = Read(path, 64); err == nil || errors.Is(err, os.ErrNotExist) {
				t.Fatal("forged state appeared valid or absent", kind, err)
			}
			if err = Publish(path, []byte(strings.Repeat("b", 32))); err == nil {
				t.Fatal("forged state repaired", kind)
			}
			after, _ := os.Lstat(path)
			if !os.SameFile(before, after) {
				t.Fatal("forged head replaced")
			}
		})
	}
}

func TestAndroidProfileValueRelativeLayoutSurvivesMoveAndStagesStayUnowned(t *testing.T) {
	path := profilePath(t)
	if err := Publish(path, []byte(strings.Repeat("a", 32))); err != nil {
		t.Fatal(err)
	}
	root := filepath.Dir(filepath.Dir(filepath.Dir(path)))
	moved := filepath.Join(t.TempDir(), "restored")
	if err := os.Rename(root, moved); err != nil {
		t.Fatal(err)
	}
	path = filepath.Join(moved, "go-control", "native-profile", "profile-id")
	if raw, err := Read(path, 64); err != nil || string(raw) != strings.Repeat("a", 32) {
		t.Fatal("move lost relative publication", err)
	}
	if UnpublishedPool(filepath.Join(filepath.Dir(path), poolName+"-foreign")) {
		t.Fatal("foreign pool recognized")
	}
	pool := filepath.Join(filepath.Dir(path), poolName)
	sum := sha256.Sum256([]byte("foreign"))
	foreign := "signer-public-pin." + hex.EncodeToString(sum[:]) + "." + strings.Repeat("b", 32)
	if err := os.WriteFile(filepath.Join(pool, foreign), []byte("foreign"), 0o600); err != nil {
		t.Fatal(err)
	}
	if UnpublishedPool(pool) {
		t.Fatal("pin stage authorized new profile birth")
	}
}

func TestAndroidProfileValueRefusesUnsafeOrExhaustedPublicationPools(t *testing.T) {
	for _, kind := range []string{"parent-mode", "parent-symlink", "pool-file", "pool-symlink", "pool-mode", "too-many-records", "foreign-record", "record-directory", "record-mode", "oversize-stage", "invalid-record-hex"} {
		t.Run(kind, func(t *testing.T) {
			path := profilePath(t)
			parent := filepath.Dir(path)
			pool := filepath.Join(parent, poolName)
			if kind == "parent-mode" {
				if err := os.Chmod(parent, 0o755); err != nil {
					t.Fatal(err)
				}
			} else if kind == "parent-symlink" {
				moved := parent + "-foreign"
				if err := os.Rename(parent, moved); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(moved, parent); err != nil {
					t.Fatal(err)
				}
			} else if kind == "pool-file" {
				if err := os.WriteFile(pool, []byte("retained foreign bytes"), 0o600); err != nil {
					t.Fatal(err)
				}
			} else if kind == "pool-symlink" {
				if err := os.Symlink(t.TempDir(), pool); err != nil {
					t.Fatal(err)
				}
			} else {
				if err := os.Mkdir(pool, 0o700); err != nil {
					t.Fatal(err)
				}
				if kind == "pool-mode" {
					if err := os.Chmod(pool, 0o755); err != nil {
						t.Fatal(err)
					}
				}
				name := "profile-id." + strings.Repeat("a", 64) + "." + strings.Repeat("b", 32)
				if kind == "invalid-record-hex" {
					name = strings.Replace(name, "a", "g", 1)
				}
				if kind == "foreign-record" {
					name = "retained-foreign"
				}
				if kind == "record-directory" {
					if err := os.Mkdir(filepath.Join(pool, name), 0o700); err != nil {
						t.Fatal(err)
					}
				} else if kind != "pool-mode" {
					value := []byte("torn stage")
					mode := os.FileMode(0o600)
					if kind == "record-mode" {
						mode = 0o644
					}
					if kind == "oversize-stage" {
						value = []byte(strings.Repeat("x", 65))
					}
					if err := os.WriteFile(filepath.Join(pool, name), value, mode); err != nil {
						t.Fatal(err)
					}
					if kind == "too-many-records" {
						for i := 1; i < maxRecords; i++ {
							name := fmt.Sprintf("profile-id.%s.%032x", strings.Repeat("a", 64), i)
							if err := os.WriteFile(filepath.Join(pool, name), value, 0o600); err != nil {
								t.Fatal(err)
							}
						}
					}
				}
			}
			if UnpublishedPool(pool) {
				t.Fatal("unsafe pre-admission stages authorized fresh creation", kind)
			}
			// Unknown orphan bytes are never read or repaired; their presence only
			// denies fresh-profile admission. Invalid pool/parent paths also deny
			// the low-level exclusive publisher independently of that preflight.
			if strings.HasPrefix(kind, "parent-") || strings.HasPrefix(kind, "pool-") || kind == "too-many-records" {
				if err := Publish(path, []byte(strings.Repeat("a", 32))); err == nil {
					t.Fatal("unsafe publication namespace admitted", kind)
				}
				if _, err := os.Lstat(path); !os.IsNotExist(err) {
					t.Fatal("unsafe publication exposed a head", err)
				}
			}
		})
	}
}

func TestAndroidProfileValueFilesystemFailuresNeverAcknowledgeNewAuthority(t *testing.T) {
	for _, kind := range []string{"parent-readonly", "pool-readonly", "ancestor-unreadable", "head-create-denied"} {
		t.Run(kind, func(t *testing.T) {
			path := profilePath(t)
			parent := filepath.Dir(path)
			root := filepath.Dir(filepath.Dir(parent))
			defer os.Chmod(parent, 0o700)
			defer os.Chmod(root, 0o700)
			switch kind {
			case "parent-readonly":
				if err := os.Chmod(parent, 0o500); err != nil {
					t.Fatal(err)
				}
			case "pool-readonly":
				pool := filepath.Join(parent, poolName)
				if err := os.Mkdir(pool, 0o500); err != nil {
					t.Fatal(err)
				}
				defer os.Chmod(pool, 0o700)
			case "ancestor-unreadable":
				if err := os.Chmod(root, 0o111); err != nil {
					t.Fatal(err)
				}
			}
			err := publishAt(path, []byte(strings.Repeat("a", 32)), func(stage string) {
				if kind == "head-create-denied" && stage == "record-directory-synced" {
					if err := os.Chmod(parent, 0o500); err != nil {
						t.Fatal(err)
					}
				}
			})
			if !errors.Is(err, os.ErrPermission) {
				t.Fatal("actual filesystem refusal not preserved", kind, err)
			}
			if _, err := os.Lstat(path); !os.IsNotExist(err) {
				t.Fatal("failed publication acquired an authoritative head", err)
			}
		})
	}
	t.Run("pre-head-directory-sync", func(t *testing.T) {
		path := profilePath(t)
		publicationChild(t, path, "early-directory-sync", false)
		if _, err := Read(path, 64); !os.IsNotExist(err) {
			t.Fatal("unacknowledged directory acquired a head", err)
		}
		if err := Publish(path, []byte(strings.Repeat("b", 32))); err != nil {
			t.Fatal("restored IO could not resume before admission", err)
		}
	})
	for _, legacy := range []bool{true, false} {
		t.Run(fmt.Sprintf("unsafe-parent-legacy-%v", legacy), func(t *testing.T) {
			path := profilePath(t)
			value := []byte(strings.Repeat("a", 32))
			if legacy {
				if err := os.WriteFile(path, value, 0o600); err != nil {
					t.Fatal(err)
				}
			} else if err := Publish(path, value); err != nil {
				t.Fatal(err)
			}
			parent := filepath.Dir(path)
			if err := os.Chmod(parent, 0o755); err != nil {
				t.Fatal(err)
			}
			defer os.Chmod(parent, 0o700)
			if _, err := Read(path, 64); !errors.Is(err, ErrInvalid) {
				t.Fatal("unsafe parent read admitted", err)
			}
		})
	}
	t.Run("head-type-changed-after-observation", func(t *testing.T) {
		path := profilePath(t)
		if err := os.WriteFile(path, []byte(strings.Repeat("a", 32)), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := readPointer(path, 64); !errors.Is(err, ErrInvalid) {
			t.Fatal("changed head type accepted as pointer", err)
		}
	})
	t.Run("overlong-pointer", func(t *testing.T) {
		path := profilePath(t)
		if err := os.Symlink(strings.Repeat("a", 300), path); err != nil {
			t.Fatal(err)
		}
		if _, err := Read(path, 64); !errors.Is(err, ErrInvalid) {
			t.Fatal("overlong pointer was read", err)
		}
	})
}
