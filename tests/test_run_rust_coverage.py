"""Coverage evidence must enumerate the same complete workspace Cargo measures."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from scripts import run_rust_coverage
from scripts.native_runtime_contract import ContractError, validate_toolchain_consumers


def test_inventory_includes_new_members_and_excludes_external_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def metadata(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert capture
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "workspace_members": ["new-member", "old-member"],
            "packages": [
                {"id": "old-member", "name": "deepseek-core"},
                {"id": "dependency", "name": "serde"},
                {"id": "new-member", "name": "deepseek-stateless-mcp"},
            ],
        }))

    monkeypatch.setattr(run_rust_coverage, "_run", metadata)
    assert run_rust_coverage._workspace_crates() == ["deepseek-core", "deepseek-stateless-mcp"]
    assert "--locked" in calls[0] and "--offline" in calls[0] and "--no-deps" in calls[0]


@pytest.mark.parametrize("members,packages", [
    ([], []),
    (["missing"], []),
    (["duplicate", "duplicate"], [{"id": "duplicate", "name": "deepseek-core"}]),
    (["malformed"], [{"id": "malformed", "name": 42}]),
])
def test_incomplete_inventory_cannot_be_reported_as_complete(
    monkeypatch: pytest.MonkeyPatch, members: list[str], packages: list[dict[str, object]],
) -> None:
    monkeypatch.setattr(run_rust_coverage, "_run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0, stdout=json.dumps({"workspace_members": members, "packages": packages}),
    ))
    with pytest.raises(ValueError, match="cargo metadata"):
        run_rust_coverage._workspace_crates()


def test_cargo_failure_does_not_fall_back_to_a_static_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_rust_coverage, "_run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 1, stdout="", stderr="locked metadata unavailable",
    ))
    with pytest.raises(RuntimeError, match="locked metadata unavailable"):
        run_rust_coverage._workspace_crates()


def test_test_inventory_reuses_coverage_builds_without_cleaning_or_running_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def inventory(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert capture
        return subprocess.CompletedProcess(command, 0, stdout="first: test\nsecond: test\nfixture: benchmark\n")

    monkeypatch.setattr(run_rust_coverage, "_run", inventory)
    assert run_rust_coverage._test_count() == 2
    command = calls[0]
    assert command[:2] == ["cargo", "llvm-cov"]
    assert {"--locked", "--workspace", "--all-features", "--no-report"}.issubset(command)
    assert command[command.index("--profile") + 1] == "coverage"
    assert command[command.index("--") + 1:] == ["--list", "--format", "terse"]


@pytest.mark.parametrize("returncode,stdout,stderr,error,message", [
    (1, "", "inventory build failed", RuntimeError, "inventory build failed"),
    (0, "not a test listing", "", ValueError, "contains no tests"),
])
def test_test_inventory_errors_are_not_reported_as_complete(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, stderr: str,
    error: type[Exception], message: str,
) -> None:
    monkeypatch.setattr(run_rust_coverage, "_run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, returncode, stdout=stdout, stderr=stderr,
    ))
    with pytest.raises(error, match=message):
        run_rust_coverage._test_count()


@pytest.mark.parametrize("removed_flag", ["--locked", "--workspace", "--all-features", "--no-report"])
def test_native_contract_rejects_incomplete_or_unlocked_coverage_inventory(
    monkeypatch: pytest.MonkeyPatch, removed_flag: str,
) -> None:
    coverage_source = Path(run_rust_coverage.__file__)
    original_read_text = Path.read_text

    def altered_inventory(path: Path, *args: Any, **kwargs: Any) -> str:
        source = original_read_text(path, *args, **kwargs)
        return source.replace(f'"{removed_flag}",', "", 1) if path == coverage_source else source

    monkeypatch.setattr(Path, "read_text", altered_inventory)
    with pytest.raises(ContractError, match="consume Cargo.lock"):
        validate_toolchain_consumers()


@pytest.mark.parametrize("profile", ["dev", "release", "diagnostic"])
def test_native_contract_rejects_inventory_using_a_different_profile(
    monkeypatch: pytest.MonkeyPatch, profile: str,
) -> None:
    coverage_source = Path(run_rust_coverage.__file__)
    original_read_text = Path.read_text

    def altered_inventory(path: Path, *args: Any, **kwargs: Any) -> str:
        source = original_read_text(path, *args, **kwargs)
        return source.replace('"coverage",', f'"{profile}",', 1) if path == coverage_source else source

    monkeypatch.setattr(Path, "read_text", altered_inventory)
    with pytest.raises(ContractError, match="consume Cargo.lock"):
        validate_toolchain_consumers()
