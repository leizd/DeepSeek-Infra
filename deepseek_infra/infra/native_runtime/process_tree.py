"""Process tree inspection and zero-Python production runtime enforcement."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

FORBIDDEN_RUNTIME_NAMES = ("python", "python3", "cpython", "pypy")


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    name: str


def _get_all_processes_windows() -> list[ProcessInfo]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    # Linux mypy stubs omit WinDLL/windll; Windows runtime still has them.
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == -1 or snapshot is None:
        return []

    entries: list[ProcessInfo] = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                name = entry.szExeFile.decode("mbcs", errors="replace")
                entries.append(
                    ProcessInfo(
                        pid=int(entry.th32ProcessID),
                        ppid=int(entry.th32ParentProcessID),
                        name=name,
                    )
                )
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    return entries


def _get_all_processes_posix(proc_dir: str = "/proc") -> list[ProcessInfo]:
    entries: list[ProcessInfo] = []
    # Try reading /proc directly on Linux
    if os.path.isdir(proc_dir):
        for entry in os.listdir(proc_dir):
            if not entry.isdigit():
                continue
            pid = int(entry)
            stat_file = os.path.join(proc_dir, entry, "stat")
            try:
                with open(stat_file, "r", encoding="utf-8", errors="replace") as f:
                    stat_content = f.read()
                # format: pid (comm) state ppid ...
                match = re.match(r"^(\d+)\s+\((.+)\)\s+[RSDZTWrsdztw]\s+(\d+)", stat_content)
                if match:
                    comm = match.group(2)
                    ppid = int(match.group(3))
                    entries.append(ProcessInfo(pid=pid, ppid=ppid, name=comm))
            except (OSError, ValueError):
                continue
        if entries:
            return entries

    # Fallback to ps -eo pid,ppid,comm
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,ppid,comm"],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in proc.stdout.splitlines()[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                entries.append(ProcessInfo(pid=int(parts[0]), ppid=int(parts[1]), name=parts[2]))
    except Exception:
        pass

    return entries


def get_all_system_processes() -> list[ProcessInfo]:
    if platform.system() == "Windows":
        return _get_all_processes_windows()
    return _get_all_processes_posix()


def get_process_descendants(
    root_pid: int,
    all_processes_fn: Callable[[], list[ProcessInfo]] = get_all_system_processes,
) -> list[ProcessInfo]:
    all_procs = all_processes_fn()
    children_map: dict[int, list[ProcessInfo]] = {}
    pid_map: dict[int, ProcessInfo] = {}

    for proc in all_procs:
        pid_map[proc.pid] = proc
        children_map.setdefault(proc.ppid, []).append(proc)

    descendants: list[ProcessInfo] = []
    if root_pid in pid_map:
        descendants.append(pid_map[root_pid])

    queue = [root_pid]
    visited = {root_pid}

    while queue:
        curr = queue.pop(0)
        for child in children_map.get(curr, []):
            if child.pid not in visited:
                visited.add(child.pid)
                descendants.append(child)
                queue.append(child.pid)

    return descendants


def find_forbidden_runtime_processes(
    root_pid: int,
    forbidden_tokens: tuple[str, ...] = FORBIDDEN_RUNTIME_NAMES,
    all_processes_fn: Callable[[], list[ProcessInfo]] = get_all_system_processes,
) -> list[ProcessInfo]:
    tree = get_process_descendants(root_pid, all_processes_fn=all_processes_fn)
    violations: list[ProcessInfo] = []
    for proc in tree:
        lowered = proc.name.lower()
        for token in forbidden_tokens:
            if token in lowered:
                violations.append(proc)
                break
    return violations


class ForbiddenPythonProcessError(RuntimeError):
    """Raised when a forbidden Python process is detected in a production process tree."""


def assert_zero_python_process_tree(
    root_pid: int,
    all_processes_fn: Callable[[], list[ProcessInfo]] = get_all_system_processes,
) -> None:
    violations = find_forbidden_runtime_processes(root_pid, all_processes_fn=all_processes_fn)
    if violations:
        details = ", ".join(f"PID {p.pid} ({p.name})" for p in violations)
        raise ForbiddenPythonProcessError(
            f"Zero-Python invariant violated: forbidden process(es) detected in process tree of PID {root_pid}: {details}"
        )
