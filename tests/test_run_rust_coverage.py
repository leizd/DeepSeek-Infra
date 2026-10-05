"""Coverage evidence must enumerate the same complete workspace Cargo measures."""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts import run_rust_coverage


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
