from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.infra.native_runtime.process_tree import (
    ForbiddenPythonProcessError,
    ProcessInfo,
    _get_all_processes_posix,
    _get_all_processes_windows,
    assert_zero_python_process_tree,
    find_forbidden_runtime_processes,
    get_all_system_processes,
    get_process_descendants,
)


def test_get_all_system_processes_returns_current_process() -> None:
    procs = get_all_system_processes()
    assert len(procs) > 0
    my_pid = os.getpid()
    assert any(p.pid == my_pid for p in procs)


def test_windows_process_enumeration_live() -> None:
    procs = _get_all_processes_windows()
    assert len(procs) > 0
    my_pid = os.getpid()
    found = [p for p in procs if p.pid == my_pid]
    assert len(found) == 1
    assert "python" in found[0].name.lower()


def test_windows_process_enumeration_null_snapshot() -> None:
    with patch("ctypes.windll.kernel32.CreateToolhelp32Snapshot", return_value=-1):
        procs = _get_all_processes_windows()
        assert procs == []


def test_posix_process_enumeration_proc_dir(tmp_path: Any) -> None:
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()

    # Valid process 100
    p100 = proc_dir / "100"
    p100.mkdir()
    (p100 / "stat").write_text("100 (deepseek-worker) S 1 100 100 0 ...", encoding="utf-8")

    # Valid child 101
    p101 = proc_dir / "101"
    p101.mkdir()
    (p101 / "stat").write_text("101 (helper) R 100 101 100 0 ...", encoding="utf-8")

    # Non-digit directory (e.g. proc/sys)
    (proc_dir / "sys").mkdir()

    # Broken stat file
    p102 = proc_dir / "102"
    p102.mkdir()
    (p102 / "stat").write_text("corrupted", encoding="utf-8")

    procs = _get_all_processes_posix(proc_dir=str(proc_dir))
    assert len(procs) == 2
    assert procs[0] == ProcessInfo(pid=100, ppid=1, name="deepseek-worker")
    assert procs[1] == ProcessInfo(pid=101, ppid=100, name="helper")


def test_posix_process_enumeration_ps_fallback() -> None:
    ps_output = "PID PPID COMM\n10 1 deepseekd\n20 10 worker\n"
    with patch("os.path.isdir", return_value=False), patch(
        "subprocess.run",
        return_value=SimpleNamespace(stdout=ps_output),
    ):
        procs = _get_all_processes_posix(proc_dir="/nonexistent/proc")
        assert len(procs) == 2
        assert procs[0] == ProcessInfo(pid=10, ppid=1, name="deepseekd")
        assert procs[1] == ProcessInfo(pid=20, ppid=10, name="worker")


def test_posix_process_enumeration_ps_failure() -> None:
    with patch("os.path.isdir", return_value=False), patch(
        "subprocess.run",
        side_effect=OSError("ps not found"),
    ):
        procs = _get_all_processes_posix(proc_dir="/nonexistent/proc")
        assert procs == []


def test_get_process_descendants_tree_traversal() -> None:
    fake_procs = [
        ProcessInfo(pid=1, ppid=0, name="init"),
        ProcessInfo(pid=10, ppid=1, name="deepseek-edge"),
        ProcessInfo(pid=20, ppid=10, name="deepseek-worker"),
        ProcessInfo(pid=30, ppid=20, name="native-helper"),
        ProcessInfo(pid=99, ppid=1, name="unrelated"),
    ]

    descendants = get_process_descendants(10, all_processes_fn=lambda: fake_procs)
    pids = [p.pid for p in descendants]
    assert pids == [10, 20, 30]


def test_get_process_descendants_handles_cycle_and_missing_root() -> None:
    # Cycle between 10 and 20
    cyclical_procs = [
        ProcessInfo(pid=10, ppid=20, name="loop1"),
        ProcessInfo(pid=20, ppid=10, name="loop2"),
    ]
    descendants = get_process_descendants(10, all_processes_fn=lambda: cyclical_procs)
    assert len(descendants) == 2

    # Missing root
    empty = get_process_descendants(999, all_processes_fn=lambda: cyclical_procs)
    assert empty == []


def test_assert_zero_python_process_tree_passes_for_pure_native() -> None:
    native_procs = [
        ProcessInfo(pid=100, ppid=1, name="deepseekd"),
        ProcessInfo(pid=200, ppid=100, name="deepseek-worker"),
        ProcessInfo(pid=300, ppid=200, name="libnative.so"),
    ]

    # No exception raised
    assert_zero_python_process_tree(100, all_processes_fn=lambda: native_procs)


def test_assert_zero_python_process_tree_detects_python_violations() -> None:
    procs_with_python = [
        ProcessInfo(pid=100, ppid=1, name="deepseek-gateway"),
        ProcessInfo(pid=200, ppid=100, name="deepseek-worker"),
        ProcessInfo(pid=300, ppid=200, name="python3.exe"),
    ]

    violations = find_forbidden_runtime_processes(100, all_processes_fn=lambda: procs_with_python)
    assert len(violations) == 1
    assert violations[0].pid == 300

    with pytest.raises(ForbiddenPythonProcessError) as exc:
        assert_zero_python_process_tree(100, all_processes_fn=lambda: procs_with_python)
    assert "PID 300 (python3.exe)" in str(exc.value)
    assert "Zero-Python invariant violated" in str(exc.value)
