"""Build capacity failures must spare unrelated processes and all cached bytes."""
from __future__ import annotations

import ctypes
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import build_backup_crypto, rust_build


@pytest.fixture(autouse=True)
def isolated_rust_cache_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "user-config/rust-build.json"
    monkeypatch.setattr(rust_build, "_user_config_path", lambda: config, raising=False)
    return config


def _saved_config(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_saved_cache_location_survives_new_builds_and_worktrees(
    tmp_path: Path, isolated_rust_cache_config: Path,
) -> None:
    cache = tmp_path / "roomy-disk/cargo-cache"
    registry = tmp_path / "roomy-disk/cargo-home"
    _saved_config(isolated_rust_cache_config, {"version": 1, "cacheRoot": str(cache), "cargoHome": str(registry)})
    settings = {"LOCALAPPDATA": str(tmp_path / "full-user-disk"), "XDG_CACHE_HOME": str(tmp_path / "full-user-disk")}
    one = rust_build.cache_environment(tmp_path / "checkout-one", env=settings, toolchain="1.85.0")
    two = rust_build.cache_environment(tmp_path / "checkout-two", env=settings, toolchain="1.85.0")
    assert Path(one["CARGO_TARGET_DIR"]).is_relative_to(cache)
    assert one["CARGO_TARGET_DIR"] == two["CARGO_TARGET_DIR"]
    assert one["CARGO_HOME"] == registry.resolve().as_posix()
    android = rust_build.cache_environment(tmp_path / "checkout-three", env=settings, toolchain="1.85.0", kind="android")
    assert Path(android["CARGO_TARGET_DIR"]).is_relative_to(cache)
    assert android["CARGO_TARGET_DIR"] != one["CARGO_TARGET_DIR"]


def test_saved_cache_respects_explicit_build_and_registry_locations(
    tmp_path: Path, isolated_rust_cache_config: Path,
) -> None:
    _saved_config(isolated_rust_cache_config, {"version": 1, "cacheRoot": str(tmp_path / "saved"),
                                              "cargoHome": str(tmp_path / "saved-registry")})
    settings = {"DEEPSEEK_RUST_CACHE_DIR": str(tmp_path / "override"), "CARGO_HOME": str(tmp_path / "override-registry")}
    actual = rust_build.cache_environment(tmp_path, env=settings)
    assert Path(actual["CARGO_TARGET_DIR"]).is_relative_to(tmp_path / "override")
    assert actual["CARGO_HOME"] == settings["CARGO_HOME"]
    explicit = rust_build.cache_environment(tmp_path, env=settings, target_dir=tmp_path / "explicit-target")
    assert explicit["CARGO_TARGET_DIR"] == (tmp_path / "explicit-target").resolve().as_posix()


@pytest.mark.parametrize("bad", [
    {"version": 2, "cacheRoot": "/unused"},
    {"version": True, "cacheRoot": "/unused"},
    {"version": 1, "cacheRoot": "relative-cache"},
    {"version": 1, "cacheRoot": ""},
    {"version": 1, "cacheRoot": "/unused", "cargoHome": "relative-registry"},
    {"version": 1, "cacheRoot": "/unused", "extra": "ignored-location"},
])
def test_invalid_saved_cache_fails_before_creating_build_output(
    tmp_path: Path, isolated_rust_cache_config: Path, bad: dict[str, Any],
) -> None:
    _saved_config(isolated_rust_cache_config, bad)
    with pytest.raises(rust_build.RustBuildConfigurationError):
        rust_build.cache_environment(tmp_path, env={})


def test_cache_reconfiguration_preserves_existing_settings_when_space_is_low(
    tmp_path: Path, isolated_rust_cache_config: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _saved_config(isolated_rust_cache_config, {"version": 1, "cacheRoot": str(tmp_path / "existing")})
    original = isolated_rust_cache_config.read_bytes()
    monkeypatch.setattr(rust_build.shutil, "disk_usage", lambda _: SimpleNamespace(free=rust_build.GIB))
    with pytest.raises(rust_build.RustDiskSpaceError):
        rust_build.configure_cache(tmp_path / "new-cache", cargo_home=tmp_path / "new-registry")
    assert isolated_rust_cache_config.read_bytes() == original
    assert not list(isolated_rust_cache_config.parent.glob("*.tmp"))


def test_configure_cache_cli_is_persistent_and_bad_config_has_no_traceback(
    tmp_path: Path, isolated_rust_cache_config: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache = tmp_path / "roomy-disk/cache"
    registry = tmp_path / "roomy-disk/registry"
    monkeypatch.setattr(rust_build.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * rust_build.GIB))
    monkeypatch.setattr(sys, "argv", ["rust_build.py", "--configure-cache-root", str(cache), "--cargo-home", str(registry)])
    assert rust_build.main() == 0
    actual = rust_build.cache_environment(tmp_path / "another-worktree", env={})
    assert Path(actual["CARGO_TARGET_DIR"]).is_relative_to(cache.resolve())
    assert actual["CARGO_HOME"] == registry.resolve().as_posix()
    isolated_rust_cache_config.write_text("invalid JSON", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rust_build.py", "--", "check", "--offline"])
    assert rust_build.main() == 2
    assert "Traceback" not in capsys.readouterr().err


def test_unreadable_cache_encoding_is_a_bounded_cli_error(
    tmp_path: Path, isolated_rust_cache_config: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    isolated_rust_cache_config.parent.mkdir(parents=True)
    isolated_rust_cache_config.write_bytes(b"\xff\xfe")
    monkeypatch.setattr(sys, "argv", ["rust_build.py", "--root", str(tmp_path), "--", "check", "--offline"])
    assert rust_build.main() == 2
    assert "Traceback" not in capsys.readouterr().err


def _environment(target: Path) -> dict[str, str]:
    return {**os.environ, "CARGO_TARGET_DIR": target.as_posix()}


def _alive(pid: int) -> bool:
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    # A killed descendant can remain as a zombie until its parent is reaped.
    status = Path(f"/proc/{pid}/stat")
    if status.is_file() and status.read_text().split(")", 1)[1].lstrip().startswith("Z"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_default_cache_is_shared_across_worktrees_and_build_output_directories(tmp_path: Path) -> None:
    settings = {"LOCALAPPDATA": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path)}
    one = rust_build.cache_environment(tmp_path / "checkout-one", env=settings, toolchain="1.85.0")
    two = rust_build.cache_environment(tmp_path / "checkout-two", env=settings, toolchain="1.85.0")
    assert one["CARGO_TARGET_DIR"] == two["CARGO_TARGET_DIR"]
    assert one["CARGO_BUILD_JOBS"] == "2"
    assert one["CARGO_INCREMENTAL"] == "0"
    android = rust_build.cache_environment(tmp_path / "checkout-three", env=settings, toolchain="1.85.0", kind="android")
    assert android["CARGO_TARGET_DIR"] != one["CARGO_TARGET_DIR"]


def test_explicit_cache_and_linker_flags_are_preserved(tmp_path: Path) -> None:
    settings = {"CARGO_TARGET_DIR": "chosen-cache", "RUSTFLAGS": "-C link-arg=chosen",
                "CARGO_BUILD_JOBS": "1", "CARGO_INCREMENTAL": "1"}
    actual = rust_build.cache_environment(tmp_path, env=settings)
    assert actual["CARGO_TARGET_DIR"] == (tmp_path / "chosen-cache").resolve().as_posix()
    assert all(actual[key] == value for key, value in settings.items() if key != "CARGO_TARGET_DIR")
    override = rust_build.cache_environment(tmp_path, env=settings, target_dir=tmp_path / "explicit")
    assert override["CARGO_TARGET_DIR"] == (tmp_path / "explicit").resolve().as_posix()


def test_cold_cache_path_stays_canonical_after_files_are_created(tmp_path: Path) -> None:
    settings = {"LOCALAPPDATA": str(tmp_path / "new-userdata"), "XDG_CACHE_HOME": str(tmp_path / "new-userdata")}
    before = rust_build.cache_environment(tmp_path, env=settings, toolchain="1.85.0")
    target = Path(before["CARGO_TARGET_DIR"])
    assert target.is_dir()
    rust_build.prepare_cache(target)
    (target / "sample.rmeta").write_bytes(b"cache-identity")
    after = rust_build.cache_environment(tmp_path, env=settings, toolchain="1.85.0")
    assert before["CARGO_TARGET_DIR"] == after["CARGO_TARGET_DIR"] == target.resolve().as_posix()
    assert (Path(after["CARGO_TARGET_DIR"]) / "sample.rmeta").read_bytes() == b"cache-identity"


def test_low_space_refuses_to_start_a_process_and_keeps_existing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    original = target / "existing.rlib"
    original.write_bytes(b"preserved")
    marker = tmp_path / "started"
    monkeypatch.setattr(rust_build.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(rust_build.RustDiskSpaceError, match="refused"):
        rust_build.run_guarded([sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(marker)) + ").touch()"],
                              cwd=tmp_path, env=_environment(target))
    assert not marker.exists()
    assert original.read_bytes() == b"preserved"


@pytest.mark.parametrize("inline", [False, True])
def test_cargo_output_override_checks_the_actual_destination_before_compiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inline: bool,
) -> None:
    output = tmp_path / "actual-output"
    rust_build.prepare_cache(output)
    rust_build.prepare_cache(tmp_path / "unused-output")
    argument = output.name if inline else output.as_posix()
    flags = ["--target-dir=" + argument] if inline else ["--target-dir", argument]
    monkeypatch.setattr(rust_build.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=0 if Path(path).resolve() == output else 10 * rust_build.GIB))

    def unexpected_start(*args: object, **kwargs: object) -> None:
        pytest.fail("Cargo started before checking its actual output partition")

    monkeypatch.setattr(rust_build.subprocess, "Popen", unexpected_start)
    with pytest.raises(rust_build.RustDiskSpaceError, match="actual-output"):
        rust_build.run_guarded(["cargo", "check", *flags], cwd=tmp_path,
                              env=_environment(tmp_path / "unused-output"))
    assert not (output / "marker").exists()


@pytest.mark.parametrize("config_file", [False, True])
@pytest.mark.parametrize("environment_override", [False, True])
def test_cargo_configuration_routes_capacity_checks_to_its_real_metadata_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_file: bool, environment_override: bool,
) -> None:
    cargo = rust_build.shutil.which("cargo")
    if cargo is None:
        pytest.skip("real Cargo metadata is required")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.rs").write_text("pub fn isolated() {}\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text('[package]\nname="disk-routing-probe"\nversion="0.1.0"\nedition="2021"\n', encoding="utf-8")
    output = tmp_path / "configured-output"
    rust_build.prepare_cache(output)
    rust_build.prepare_cache(tmp_path / "unused-output")
    settings = 'build.target-dir="' + output.as_posix() + '"'
    if config_file:
        config = tmp_path / "output.toml"
        config.write_text(settings + "\n", encoding="utf-8")
        settings = config.as_posix()
    environment = _environment(tmp_path / "unused-output")
    # Cargo 1.85 gives CARGO_TARGET_DIR priority over build.target-dir, including
    # command-line --config. Probe the real tool rather than inventing precedence.
    expected = tmp_path / "unused-output" if environment_override else output
    if not environment_override:
        environment.pop("CARGO_TARGET_DIR")
    monkeypatch.setattr(rust_build.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=0 if Path(path).resolve() == expected else 10 * rust_build.GIB))
    original_start = rust_build.subprocess.Popen

    def only_metadata(command: list[str], *args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        if "metadata" not in command:
            pytest.fail("Compilation started before checking the configured output partition")
        return original_start(command, *args, **kwargs)

    monkeypatch.setattr(rust_build.subprocess, "Popen", only_metadata)
    with pytest.raises(rust_build.RustDiskSpaceError, match=expected.name):
        rust_build.run_guarded([cargo, "check", "--config", settings], cwd=tmp_path,
                              env=environment)
    assert not (output / "debug").exists()


def test_relative_cargo_environment_uses_the_build_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = tmp_path / "caller"
    workspace = tmp_path / "workspace"
    rust_build.prepare_cache(caller / "relative-output")
    actual = workspace / "relative-output"
    rust_build.prepare_cache(actual)
    monkeypatch.chdir(caller)
    monkeypatch.setattr(rust_build.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=0 if Path(path).resolve() == actual else 10 * rust_build.GIB))

    def unexpected_start(*args: object, **kwargs: object) -> None:
        pytest.fail("Cargo started before checking its relative output directory")

    monkeypatch.setattr(rust_build.subprocess, "Popen", unexpected_start)
    with pytest.raises(rust_build.RustDiskSpaceError, match="workspace"):
        rust_build.run_guarded(["cargo", "check"], cwd=workspace, env=_environment(Path("relative-output")))


def test_capacity_refusal_has_a_stable_cli_exit_code_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["rust_build.py", "--root", str(tmp_path), "--target-dir", str(tmp_path / "target"),
                                     "--", "check"])

    def refuse(*args: object, **kwargs: object) -> None:
        raise rust_build.RustDiskSpaceError("Rust build refused: capacity probe")

    monkeypatch.setattr(rust_build, "run_guarded", refuse)
    assert rust_build.main() == rust_build.DISK_FULL_EXIT
    captured = capsys.readouterr()
    assert "capacity probe" in captured.err and "Traceback" not in captured.err


@pytest.mark.parametrize("toolchain", [False, True])
def test_download_partition_is_checked_before_starting_cargo_or_rustup_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toolchain: bool,
) -> None:
    home = tmp_path / "download-cache"
    home.mkdir()
    environment = {**_environment(tmp_path / "target"), "CARGO_HOME": home.as_posix(),
                   "RUSTUP_HOME": home.as_posix(), "RUSTUP_AUTO_INSTALL": "1"}
    monkeypatch.setattr(rust_build.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=0 if Path(path) == home else 10 * rust_build.GIB))

    def unexpected_start(*args: object, **kwargs: object) -> None:
        pytest.fail("A download-capable process started on a full partition")

    monkeypatch.setattr(rust_build.subprocess, "Popen", unexpected_start)
    arguments = ["+1.85.0", "check", "--offline"] if toolchain else ["check"]
    with pytest.raises(rust_build.RustDiskSpaceError, match="download-cache"):
        rust_build.run_guarded(["cargo", *arguments, "--config", 'build.target-dir="chosen"'],
                              cwd=tmp_path, env=environment)


@pytest.mark.parametrize("selection", ["argument", "environment"])
def test_missing_toolchain_never_downloads_implicitly_during_a_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str,
) -> None:
    cargo = rust_build.shutil.which("cargo")
    if cargo is None or rust_build.shutil.which("rustup") is None:
        pytest.skip("real rustup Cargo proxy is required")
    requests: list[str] = []

    class DistributionServer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_error(404)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), DistributionServer)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    environment = {**_environment(tmp_path / "target"), "RUSTUP_HOME": (tmp_path / "rustup").as_posix(),
                   "RUSTUP_DIST_SERVER": f"http://127.0.0.1:{server.server_port}", "CARGO_NET_OFFLINE": "true"}
    environment.pop("RUSTUP_AUTO_INSTALL", None)
    environment.pop("RUSTUP_TOOLCHAIN", None)
    command = [cargo, "--version"]
    if selection == "argument":
        command.insert(1, "+9.99.0")
    else:
        environment["RUSTUP_TOOLCHAIN"] = "9.99.0"
    monkeypatch.setattr(rust_build.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * rust_build.GIB))
    try:
        result = rust_build.run_guarded(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=5)
        assert result.returncode != 0
        assert "not installed" in result.stderr
        assert requests == [], "The build contacted the toolchain distribution server"
        assert not list((tmp_path / "rustup/downloads").glob("*"))
        assert "RUSTUP_AUTO_INSTALL" not in environment
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_metadata_download_is_stopped_if_its_partition_fills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "rustup"
    home.mkdir()
    marker = home / "download-started"
    target = tmp_path / "configured-target"
    environment = {**_environment(tmp_path / "unused-target"), "RUSTUP_AUTO_INSTALL": "1",
                   "CARGO_NET_OFFLINE": "true", "RUSTUP_HOME": home.as_posix()}
    metadata = json.dumps({"target_directory": target.as_posix()})
    code = ("import time; from pathlib import Path; Path(" + repr(marker.as_posix()) + ").touch(); "
            "time.sleep(0.6); print(" + repr(metadata) + ")")
    original_start = rust_build.subprocess.Popen

    def metadata_only(command: list[str], *args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        if "metadata" not in command:
            pytest.fail("Compilation started after the toolchain download partition filled")
        if "--child" in command:
            command = [*command[:4], sys.executable, "-c", code]
        else:
            command = [sys.executable, "-c", code]
        return original_start(command, *args, **kwargs)

    monkeypatch.setattr(rust_build.subprocess, "Popen", metadata_only)
    monkeypatch.setattr(rust_build.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=0 if Path(path) == home and marker.exists() else 10 * rust_build.GIB))
    with pytest.raises(rust_build.RustDiskSpaceError, match="Rust build stopped") as error:
        rust_build.run_guarded(["cargo", "+9.99.0", "check", "--config", 'build.target-dir="configured-target"'],
                              cwd=tmp_path, env=environment, poll_seconds=0.01)
    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)
    assert error.value.__cause__.returncode == rust_build.DISK_FULL_EXIT
    assert not (target / "debug").exists()


def test_missing_storage_root_fails_without_an_infinite_parent_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unavailable(_: Path) -> bool:
        nonlocal calls
        calls += 1
        assert calls < 30, "Parent walk did not stop at the filesystem root"
        return False

    # Restore Path.exists before pytest formats an intentional pre-fix failure.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "exists", unavailable)
        with pytest.raises(rust_build.RustBuildConfigurationError, match="storage"):
            rust_build._additional_storage_paths(["cargo", "check"], tmp_path,
                                                 {"CARGO_HOME": (tmp_path / "missing").as_posix(), "RUSTUP_AUTO_INSTALL": "0"})


@pytest.mark.parametrize("offline", ["flag", "environment"])
def test_offline_cargo_does_not_require_space_on_the_registry_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: str,
) -> None:
    cargo = rust_build.shutil.which("cargo")
    if cargo is None:
        pytest.skip("real Cargo is required")
    home = tmp_path / "registry"
    home.mkdir()
    environment = {**_environment(tmp_path / "target"), "CARGO_HOME": home.as_posix()}
    command = [cargo, "--version"]
    if offline == "flag":
        command.append("--offline")
    else:
        environment["CARGO_NET_OFFLINE"] = "true"
    monkeypatch.setattr(rust_build.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=0 if Path(path) == home else 10 * rust_build.GIB))
    result = rust_build.run_guarded(command, cwd=tmp_path, env=environment, capture_output=True, text=True, check=True)
    assert result.stdout.startswith("cargo ")


def test_running_capacity_guard_kills_owned_parent_and_child_but_spares_unrelated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pid = tmp_path / "child.pid"
    parent_pid = tmp_path / "parent.pid"
    child_code = "import os,time; from pathlib import Path; Path(" + repr(str(child_pid)) + ").write_text(str(os.getpid())); time.sleep(60)"
    parent_code = ("import os,subprocess,sys,time; from pathlib import Path; Path(" + repr(str(parent_pid)) +
                   ").write_text(str(os.getpid())); subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "]); time.sleep(60)")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(rust_build.shutil, "disk_usage",
                        lambda _: SimpleNamespace(free=0 if child_pid.is_file() and child_pid.stat().st_size else 10 * rust_build.GIB))
    try:
        result = rust_build.run_guarded([sys.executable, "-c", parent_code], cwd=tmp_path, env=_environment(tmp_path / "target"),
                                       capture_output=True, text=True, timeout=10, poll_seconds=0.05)
        assert result.returncode == rust_build.DISK_FULL_EXIT
        assert "reserve protected" in result.stderr
        assert not _alive(int(child_pid.read_text()))
        assert not _alive(int(parent_pid.read_text()))
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=10)


def test_success_preserves_output_and_exit_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rust_build.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * rust_build.GIB))
    result = rust_build.run_guarded([sys.executable, "-c", "import sys; print('stdout'); print('stderr',file=sys.stderr); sys.exit(7)"],
                                   cwd=tmp_path, env=_environment(tmp_path / "target"), capture_output=True, text=True)
    assert result.returncode == 7 and result.stdout.strip() == "stdout" and result.stderr.strip() == "stderr"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_build_temporary_files_use_the_guarded_partition_and_are_cleaned_after_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int,
) -> None:
    target = tmp_path / "target"
    unrelated = tmp_path / "caller-temp"
    unrelated.mkdir()
    marker = unrelated / "retained.txt"
    marker.write_bytes(b"retained")
    environment = {**_environment(target), **{key: str(unrelated) for key in ("TMPDIR", "TEMP", "TMP", "RUSTC_TMPDIR")}}
    before = dict(environment)
    monkeypatch.setattr(rust_build.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * rust_build.GIB))
    code = ("import json,os,sys,tempfile; from pathlib import Path; "
            "p=Path(tempfile.gettempdir()); (p/'linker-object.o').write_bytes(b'owned'); "
            "print(json.dumps({k:os.environ[k] for k in ('TMPDIR','TEMP','TMP','RUSTC_TMPDIR')})); "
            f"sys.exit({exit_code})")
    result = rust_build.run_guarded([sys.executable, "-c", code], cwd=tmp_path, env=environment,
                                   capture_output=True, text=True)
    assert result.returncode == exit_code
    directories = json.loads(result.stdout)
    assert len(set(directories.values())) == 1
    scratch = Path(directories["TEMP"])
    assert scratch.parent == target.resolve() / ".deepseek-build-tmp"
    assert not scratch.exists()
    assert environment == before
    assert marker.read_bytes() == b"retained" and sorted(path.name for path in unrelated.iterdir()) == ["retained.txt"]


def test_parallel_builds_have_independent_temporary_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rust_build.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * rust_build.GIB))
    target = tmp_path / "target"
    code = ("import tempfile,time; from pathlib import Path; p=Path(tempfile.gettempdir()); "
            "(p/'object.o').write_bytes(b'owned'); time.sleep(0.15); assert (p/'object.o').read_bytes()==b'owned'; print(p)")

    def build(_: int) -> str:
        result = rust_build.run_guarded([sys.executable, "-c", code], cwd=tmp_path, env=_environment(target),
                                       capture_output=True, text=True, check=True)
        return result.stdout.strip()

    with ThreadPoolExecutor(max_workers=2) as pool:
        directories = list(pool.map(build, range(2)))
    assert len(set(directories)) == 2
    assert all(Path(path).parent == target.resolve() / ".deepseek-build-tmp" for path in directories)
    assert all(not Path(path).exists() for path in directories)


def test_timeout_cleans_only_owned_temporary_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rust_build.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * rust_build.GIB))
    target = tmp_path / "target"
    rust_build.prepare_cache(target)
    original = target / "existing.rlib"
    original.write_bytes(b"preserved")
    code = "import tempfile,time; from pathlib import Path; (Path(tempfile.gettempdir())/'object.o').write_bytes(b'owned'); time.sleep(60)"
    result = rust_build.run_guarded([sys.executable, "-c", code], cwd=tmp_path, env=_environment(target),
                                   capture_output=True, text=True, timeout=0.5, poll_seconds=0.05)
    assert result.returncode == rust_build.DISK_FULL_EXIT and "timeout" in result.stderr
    assert original.read_bytes() == b"preserved"
    assert not list((target / ".deepseek-build-tmp").iterdir())


@pytest.mark.parametrize("relative_root", [True, False])
@pytest.mark.parametrize("release", [True, False])
def test_packaging_build_uses_shared_cache_and_valid_manifest_from_either_root_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_root: bool, release: bool,
) -> None:
    project = tmp_path / "project"
    (project / "rust").mkdir(parents=True)
    manifest = project / "rust/Cargo.toml"
    manifest.write_text("[workspace]\n", encoding="utf-8")
    cache = tmp_path / "shared-cache"
    monkeypatch.setenv("CARGO_TARGET_DIR", cache.as_posix())
    monkeypatch.chdir(tmp_path)
    suffix = ".exe" if os.name == "nt" else ""

    def build(command: list[str], *, cwd: Path, env: dict[str, str], **kwargs: object) -> None:
        selected = Path(command[command.index("--manifest-path") + 1])
        assert (cwd / selected).resolve() == manifest.resolve()
        assert cwd == project / "rust"
        assert Path(env["CARGO_TARGET_DIR"]) == cache.resolve()
        assert ("--release" in command) is release
        output = cache / ("release" if release else "debug")
        output.mkdir(parents=True)
        for name in ("backup-crypto", "deepseek-backup"):
            (output / (name + suffix)).write_bytes(name.encode())

    monkeypatch.setattr(build_backup_crypto, "run_guarded", build)
    root = Path("project") if relative_root else project
    result = build_backup_crypto.build_backup_crypto(root, release=release)
    assert result == project / "bin" / ("backup-crypto" + suffix)
    for name in ("backup-crypto", "deepseek-backup"):
        assert (project / "bin" / (name + suffix)).read_bytes() == name.encode()


@pytest.mark.skipif(os.name != "nt", reason="NTFS directory inheritance")
def test_new_cache_files_inherit_lossless_compression(tmp_path: Path) -> None:
    target = tmp_path / "target"
    rust_build.prepare_cache(target)
    content = bytes(range(256)) * 2048
    binary = target / "new.rlib"
    binary.write_bytes(content)
    assert target.stat().st_file_attributes & 0x800
    assert binary.stat().st_file_attributes & 0x800
    assert binary.read_bytes() == content
