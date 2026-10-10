"""Shared Cargo cache and disk reserve for native build entry points.

Python is an offline build supervisor only. No cache files are deleted.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

GIB = 1024**3
START_FREE_BYTES = 2 * GIB
RESERVE_BYTES = GIB
DISK_FULL_EXIT = 86


class RustDiskSpaceError(RuntimeError):
    """The build would consume the disk reserve."""


class RustBuildConfigurationError(RuntimeError):
    """Cargo's output destination cannot be established safely."""


def _signal_group(pid: int, *, force: bool = False) -> None:
    # Unix-only APIs are absent from Windows type stubs and never run there.
    getattr(os, "killpg")(pid, getattr(signal, "SIGKILL") if force else signal.SIGTERM)


def _user_config_path() -> Path:
    # HOME is stable across MSIX and ordinary terminals; LOCALAPPDATA is not.
    return Path.home() / ".config/deepseek-infra/rust-build.json"


def _saved_cache_settings() -> dict[str, str]:
    config = _user_config_path()
    try:
        with config.open(encoding="utf-8") as stream:
            raw = stream.read(16385)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as error:
        raise RustBuildConfigurationError(f"Cannot read Rust cache settings: {config.as_posix()}") from error
    try:
        document = json.loads(raw)
        if (len(raw) > 16384 or not isinstance(document, dict)
                or type(document.get("version")) is not int or document["version"] != 1
                or set(document) - {"version", "cacheRoot", "cargoHome"} or "cacheRoot" not in document):
            raise ValueError("Invalid cache settings format")
        settings = {}
        for key in ("cacheRoot", "cargoHome"):
            if key not in document:
                continue
            value = document[key]
            if not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_absolute():
                raise ValueError(f"{key} must be an absolute path")
            settings[key] = Path(value).expanduser().resolve().as_posix()
        return settings
    except (ValueError, TypeError) as error:
        raise RustBuildConfigurationError(f"Invalid Rust cache settings: {config.as_posix()}") from error


def configure_cache(cache_root: Path, *, cargo_home: Path | None = None) -> dict[str, Any]:
    """Persist shared build locations without moving or deleting existing files."""
    document: dict[str, Any] = {"version": 1}
    paths = {"cacheRoot": cache_root, **({"cargoHome": cargo_home} if cargo_home is not None else {})}
    for key, path in paths.items():
        path = path.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        _check_start_space([path], START_FREE_BYTES)
        prepare_cache(path)
        document[key] = path.as_posix()
    config = _user_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="rust-build-", suffix=".tmp",
                                         dir=config.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, config)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return document


def cache_environment(
    root: Path, *, env: dict[str, str] | None = None, toolchain: str = "current",
    target_dir: Path | None = None, kind: str = "native",
) -> dict[str, str]:
    result = dict(os.environ if env is None else env)
    settings = _saved_cache_settings()
    if settings.get("cargoHome"):
        result.setdefault("CARGO_HOME", settings["cargoHome"])
    explicit = target_dir or (Path(result["CARGO_TARGET_DIR"]) if result.get("CARGO_TARGET_DIR") else None)
    if explicit is not None:
        target = explicit if explicit.is_absolute() else root / explicit
    else:
        cache = result.get("DEEPSEEK_RUST_CACHE_DIR") or settings.get("cacheRoot")
        if cache:
            base = Path(cache).expanduser()
            if not base.is_absolute():
                base = root / base
        elif os.name == "nt":
            base = Path(result.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "DeepSeekInfra/cargo"
        else:
            base = Path(result.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "deepseek-infra/cargo"
        key = re.sub(r"[^a-zA-Z0-9._-]", "_", toolchain)
        target = base / (platform.system().lower() + "-" + platform.machine().lower()) / key / kind / "target"
    # MSIX can redirect a newly created LOCALAPPDATA directory into its package
    # cache. Resolve after creation, so the first and later builds use the same
    # physical directory instead of switching path identities after a cold build.
    target.mkdir(parents=True, exist_ok=True)
    result["CARGO_TARGET_DIR"] = target.resolve().as_posix()
    result.setdefault("CARGO_INCREMENTAL", "0")
    result.setdefault("CARGO_BUILD_JOBS", "2")
    # Building must not silently install a missing multi-GB toolchain.
    result.setdefault("RUSTUP_AUTO_INSTALL", "0")
    # Cargo invoked from the repository root does not load rust/.cargo/config.
    # Preserve explicit RUSTFLAGS and the platform-specific linker configuration.
    if os.name == "nt":
        result.setdefault("CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUSTFLAGS", "-C link-self-contained=yes")
    return result


def prepare_cache(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Mark this build directory for lossless NTFS compression of future files.
        # Do not recursively modify arbitrary caller-selected existing content.
        compact = Path(os.environ["SystemRoot"]) / "System32/compact.exe"
        if not target.stat().st_file_attributes & 0x800:
            subprocess.run([str(compact), "/C", "/Q", "."], cwd=target,
                           stdout=subprocess.DEVNULL, check=True)


def _cargo_output_environment(
    command: list[str], cwd: Path, env: dict[str, str], *, storage_paths: list[Path],
    reserve_bytes: int, poll_seconds: float,
) -> dict[str, str]:
    if not command or Path(command[0]).stem.lower() != "cargo":
        return env
    # Arguments after -- belong to the test/program, not to Cargo.
    arguments = command[1:command.index("--")] if "--" in command else command[1:]
    overrides: dict[str, str] = {}
    configs: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        option, separator, value = argument.partition("=")
        if option in {"--target-dir", "--config", "--manifest-path"}:
            if not separator:
                index += 1
                if index == len(arguments):
                    raise RustBuildConfigurationError(f"Missing value for Cargo {option}")
                value = arguments[index]
            if option == "--config":
                configs.extend(["--config", value])
            else:
                overrides[option] = value
        index += 1
    selected = overrides.get("--target-dir")
    if selected is None and configs:
        # Cargo resolves inline TOML, config files, precedence and relative paths.
        # Metadata is offline/locked, has no build scripts and starts no compiler.
        toolchain = arguments[:1] if arguments[:1] and arguments[0].startswith("+") else []
        manifest = ["--manifest-path", overrides["--manifest-path"]] if "--manifest-path" in overrides else []
        try:
            result = _run_process_guarded([command[0], *toolchain, "metadata", "--format-version=1", "--no-deps",
                                           "--locked", "--offline", *manifest, *configs],
                                          cwd=cwd, env=env, storage_paths=storage_paths,
                                          capture_output=True, text=True, check=True, timeout=15,
                                          reserve_bytes=reserve_bytes, poll_seconds=poll_seconds)
            selected = json.loads(result.stdout)["target_directory"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            if isinstance(error, subprocess.CalledProcessError) and error.returncode == DISK_FULL_EXIT:
                raise RustDiskSpaceError(error.stderr.strip() or "Rust metadata exceeded the guarded reserve") from error
            raise RustBuildConfigurationError("Cannot establish Cargo's configured output directory; compilation refused") from error
    if selected is None:
        return env
    target = Path(selected)
    if not target.is_absolute():
        target = cwd / target
    target.mkdir(parents=True, exist_ok=True)
    return {**env, "CARGO_TARGET_DIR": target.resolve().as_posix()}


class _WindowsJob:
    """A gated child is assigned before it can spawn Cargo or other descendants."""

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                        ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcesses", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("Basic", BasicLimits), ("IO", ctypes.c_ulonglong * 6),
                        ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                        ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, pid: int) -> None:
        handle = self.kernel.OpenProcess(0x100 | 0x1, False, pid)  # SET_QUOTA | TERMINATE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(handle)

    def terminate(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, DISK_FULL_EXIT):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _additional_storage_paths(command: list[str], cwd: Path, env: dict[str, str]) -> list[Path]:
    paths = []
    if command and Path(command[0]).stem.lower() in {"cargo", "cargo-llvm-cov"}:
        arguments = command[1:command.index("--")] if "--" in command else command[1:]
        homes = {}
        if "--offline" not in arguments and env.get("CARGO_NET_OFFLINE", "").lower() not in {"true", "1"}:
            homes["CARGO_HOME"] = ".cargo"
        # Opt-in auto-install also applies to RUSTUP_TOOLCHAIN and directory overrides.
        if env.get("RUSTUP_AUTO_INSTALL", "1") != "0":
            homes["RUSTUP_HOME"] = ".rustup"
        for key, default in homes.items():
            path = Path(env[key]).expanduser() if env.get(key) else Path.home() / default
            if not path.is_absolute():
                path = cwd / path
            while not path.exists():
                if path.parent == path:
                    raise RustBuildConfigurationError(f"Rust storage root is unavailable: {path.as_posix()}")
                path = path.parent
            paths.append(path.resolve())
    return paths


def _check_start_space(paths: list[Path], start_free_bytes: int) -> None:
    for path in paths:
        free = shutil.disk_usage(path).free
        if free < start_free_bytes:
            raise RustDiskSpaceError(f"Rust build refused: {path} has {free / GIB:.2f} GiB free; needs {start_free_bytes / GIB:.2f} GiB")


def run_guarded(
    command: list[str], *, cwd: Path, env: dict[str, str], capture_output: bool = False,
    text: bool = False, check: bool = False, timeout: float | None = None,
    start_free_bytes: int = START_FREE_BYTES, reserve_bytes: int = RESERVE_BYTES,
    poll_seconds: float = 1.0, announce_cache: bool = False,
) -> subprocess.CompletedProcess[Any]:
    # Even read-only Cargo metadata can make rustup install a missing toolchain.
    env = {**env}
    env.setdefault("RUSTUP_AUTO_INSTALL", "0")
    storage_paths = _additional_storage_paths(command, cwd, env)
    _check_start_space(storage_paths, start_free_bytes)
    env = _cargo_output_environment(command, cwd, env, storage_paths=storage_paths,
                                    reserve_bytes=reserve_bytes, poll_seconds=poll_seconds)
    target = Path(env["CARGO_TARGET_DIR"])
    if not target.is_absolute():
        target = cwd / target
    target.mkdir(parents=True, exist_ok=True)
    target = target.resolve()
    env = {**env, "CARGO_TARGET_DIR": target.as_posix()}
    if announce_cache:
        print(f"Rust cache: {target.as_posix()}", flush=True)
    prepare_cache(target)
    _check_start_space([target], start_free_bytes)
    storage_paths.insert(0, target)
    scratch_root = target / ".deepseek-build-tmp"
    scratch_root.mkdir(exist_ok=True)
    # Never follow a caller-created junction into an unmonitored partition.
    if scratch_root.resolve() != scratch_root:
        raise RustBuildConfigurationError("Rust temporary directory must stay inside the guarded cache; compilation refused")
    with tempfile.TemporaryDirectory(prefix="build-", dir=scratch_root) as scratch:
        child_env = {**env, **{key: Path(scratch).as_posix() for key in ("TMPDIR", "TEMP", "TMP", "RUSTC_TMPDIR")}}
        return _run_process_guarded(command, cwd=cwd, env=child_env, storage_paths=storage_paths,
                                    capture_output=capture_output, text=text, check=check, timeout=timeout,
                                    reserve_bytes=reserve_bytes, poll_seconds=poll_seconds)


def _run_process_guarded(
    command: list[str], *, cwd: Path, env: dict[str, str], storage_paths: list[Path],
    capture_output: bool, text: bool, check: bool, timeout: float | None,
    reserve_bytes: int, poll_seconds: float,
) -> subprocess.CompletedProcess[Any]:
    job = _WindowsJob() if os.name == "nt" else None
    actual = [sys.executable, str(Path(__file__).resolve()), "--child", str(cwd), *command] if job else command
    process: subprocess.Popen[Any] | None = None
    communication: list[tuple[Any, Any]] = []
    communication_errors: list[BaseException] = []
    started = time.monotonic()
    reason = ""
    try:
        process = subprocess.Popen(actual, cwd=cwd, env=env, text=text, start_new_session=job is None,
                                   stdin=subprocess.PIPE if job else None,
                                   stdout=subprocess.PIPE if capture_output else None,
                                   stderr=subprocess.PIPE if capture_output else None)
        if job:
            job.assign(process.pid)

        def communicate() -> None:
            try:
                gate = ("\n" if text else b"\n") if job else None
                communication.append(process.communicate(input=gate))
            except BaseException as error:
                communication_errors.append(error)

        reader = threading.Thread(target=communicate, daemon=True)
        reader.start()
        while process.poll() is None:
            for path in storage_paths:
                free = shutil.disk_usage(path).free
                if free < reserve_bytes:
                    reason = f"Rust build stopped: {path} has {free / GIB:.2f} GiB free; {reserve_bytes / GIB:.2f} GiB reserve protected"
                    break
            if reason:
                break
            if timeout is not None and time.monotonic() - started > timeout:
                reason = f"Rust build exceeded timeout of {timeout} seconds"
                break
            reader.join(poll_seconds)
        if reason:
            if job:
                job.terminate()
            else:
                _signal_group(process.pid)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _signal_group(process.pid, force=True)
        reader.join()
        if communication_errors:
            raise communication_errors[0]
        stdout, stderr = communication[0]
        code = DISK_FULL_EXIT if reason else process.returncode
        if reason:
            print(reason, file=sys.stderr, flush=True)
            if capture_output:
                if text:
                    stderr = (stderr if isinstance(stderr, str) else "") + reason + "\n"
                else:
                    stderr = (stderr if isinstance(stderr, bytes) else b"") + (reason + "\n").encode()
        result = subprocess.CompletedProcess(command, code, stdout, stderr)
        if check:
            result.check_returncode()
        return result
    finally:
        if job:
            # Includes descendant rustc/linker/test processes, never unrelated apps.
            job.close()
            if process is not None and process.poll() is None:
                # Assignment errors leave only the gated supervisor, before Cargo.
                process.kill()
                process.wait(timeout=5)
        elif process is not None and process.poll() is None:
            _signal_group(process.pid, force=True)
            process.wait()


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        if sys.stdin.buffer.readline() not in {b"\n", b"\r\n"}:
            return DISK_FULL_EXIT
        return subprocess.call(sys.argv[3:], cwd=sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--toolchain")
    parser.add_argument("--target-dir", type=Path)
    parser.add_argument("--configure-cache-root", type=Path, help="Save the shared Cargo build-cache root for this user")
    parser.add_argument("--cargo-home", type=Path, help="Save a dependency-cache location with --configure-cache-root")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Cargo arguments after -- (for example test --workspace --locked)")
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.configure_cache_root is not None and (command or args.target_dir or args.toolchain):
        parser.error("cache configuration is separate from compiling")
    if args.cargo_home is not None and args.configure_cache_root is None:
        parser.error("--cargo-home requires --configure-cache-root")
    if not command and args.configure_cache_root is None:
        parser.error("supply a Cargo command after --")
    root = args.root.resolve()
    cargo = ["cargo", *(["+" + args.toolchain] if args.toolchain else [])]
    try:
        if args.configure_cache_root is not None:
            document = configure_cache(args.configure_cache_root, cargo_home=args.cargo_home)
            print(json.dumps({"settingsFile": _user_config_path().as_posix(), **document}, ensure_ascii=False))
            return 0
        environment = cache_environment(root, toolchain=args.toolchain or "current", target_dir=args.target_dir)
        return run_guarded([*cargo, *command], cwd=root / "rust", env=environment, announce_cache=True).returncode
    except RustDiskSpaceError as error:
        print(error, file=sys.stderr, flush=True)
        return DISK_FULL_EXIT
    except RustBuildConfigurationError as error:
        print(error, file=sys.stderr, flush=True)
        return 2
    except OSError as error:
        print(f"Cannot prepare Rust build storage: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
