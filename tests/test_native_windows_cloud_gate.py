"""Offline gate/parser oracles; these never launch or qualify native Windows UI."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess

import pytest
import yaml

from scripts.verify_native_windows_cloud import (
    InitializedFilesystemTarget,
    TARGET_ID,
    active_owned_pids,
    canonical_source,
    inspect_window_events,
    require_cloud_windows,
    target_witnesses,
    validate_window_receipt,
)


def cloud_environment() -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Windows",
        "GITHUB_REPOSITORY": "leizd/DeepSeek-Infra", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "12345",
    }


def test_windows_native_cloud_gate_requires_the_explicit_host() -> None:
    require_cloud_windows(cloud_environment(), "win32")
    pushed = dict(cloud_environment(), GITHUB_EVENT_NAME="push", GITHUB_REF_NAME="codex/rust-cloud/control-rpc-mtls-isolated")
    require_cloud_windows(pushed, "win32")
    pushed["GITHUB_REF_NAME"] = "main"
    with pytest.raises(RuntimeError, match="WINDOWS_CLOUD_HOST_REQUIRED"):
        require_cloud_windows(pushed, "win32")
    with pytest.raises(RuntimeError, match="WINDOWS_CLOUD_HOST_REQUIRED"):
        require_cloud_windows(cloud_environment(), "linux")


@pytest.mark.parametrize(("name", "value"), [
    ("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"), ("RUNNER_OS", "Linux"),
    ("GITHUB_REPOSITORY", "other/repository"), ("GITHUB_EVENT_NAME", "pull_request"),
    ("GITHUB_SHA", "short"), ("GITHUB_RUN_ID", ""),
])
def test_windows_gate_refuses_local_or_unidentified_execution(name: str, value: str) -> None:
    environment = cloud_environment()
    environment[name] = value
    with pytest.raises(RuntimeError, match="WINDOWS_CLOUD_HOST_REQUIRED"):
        require_cloud_windows(environment, "win32")


def window_receipt() -> dict[str, object]:
    checks: dict[str, object] = {key: True for key in ("react", "desktop", "fileId", "readerText", "originalEqual", "layout")}
    checks.update({key: 200 for key in ("configStatus", "uploadStatus", "readerStatus", "sourceStatus")})
    checks["retainedFileId"] = "isolated-file-id"
    return {"checks": checks, "releaseQualified": False, "platform": "windows-amd64-webview2"}


def test_window_receipt_requires_a_retained_nonempty_workload() -> None:
    assert validate_window_receipt(window_receipt()) == "isolated-file-id"


@pytest.mark.parametrize(("name", "value"), [
    ("react", False), ("originalEqual", False), ("sourceStatus", 401), ("retainedFileId", ""),
])
def test_window_receipt_cannot_replace_failed_behavior_with_a_green_wrapper(name: str, value: object) -> None:
    receipt = window_receipt()
    checks = receipt["checks"]
    assert isinstance(checks, dict)
    checks[name] = value
    with pytest.raises(RuntimeError, match="WINDOWS_WEBVIEW_WORKLOAD_INCOMPLETE"):
        validate_window_receipt(receipt)


TEST = "TestNativeWindowLoadsAuthenticatedReactAndRunsFileWorkload"
PACKAGE = "github.com/leizd/DeepSeek-Infra/go/internal/desktop"


def events(path: Path, terminal: str | None) -> None:
    rows = [dict(Action="run", Package=PACKAGE, Test=TEST)]
    if terminal:
        rows += [dict(Action=terminal, Package=PACKAGE, Test=TEST), dict(Action=terminal, Package=PACKAGE)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_real_window_test_requires_a_complete_uncached_terminal_record(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events(path, "pass")
    assert inspect_window_events(path)["topLevelPassed"] == 1


@pytest.mark.parametrize("terminal", [None, "skip", "fail"])
def test_window_parser_refuses_missing_skipped_or_failed_native_execution(tmp_path: Path, terminal: str | None) -> None:
    path = tmp_path / "events.jsonl"
    events(path, terminal)
    with pytest.raises(RuntimeError, match="WINDOWS_NATIVE_TEST_INCOMPLETE"):
        inspect_window_events(path)


def test_native_shutdown_cannot_hide_a_retained_child_after_its_parent_exits() -> None:
    known_tree = {100, 101, 102}
    # Launcher100 and control101 have exited, but the originally observed worker
    # remains. A traversal from the now-missing parent cannot establish cleanup.
    inventory = [{"pid": 102, "parent": 101, "executable": "deepseek-worker"},
                 {"pid": 900, "parent": 800, "executable": "unrelated-process"}]
    assert active_owned_pids(known_tree, inventory) == {102}
    assert active_owned_pids(known_tree, inventory[1:]) == set()


def test_target_witnesses_require_original_registration_relocation_and_initialization(tmp_path: Path) -> None:
    directory = tmp_path / "go-control"
    directory.mkdir()
    with closing(sqlite3.connect(directory / "control.sqlite3")) as database:
        database.executescript(
            """
            CREATE TABLE control_target_registrations (
                operation_id TEXT, target_id TEXT, revision INTEGER, public_generation INTEGER, observation BLOB
            );
            CREATE TABLE control_operator_mutations (
                operation_id TEXT, action_id TEXT, execution_epoch INTEGER, writer_fencing_token INTEGER
            );
            CREATE TABLE control_target_registration_locations (operation_id TEXT, observation BLOB);
            CREATE TABLE control_target_marker_operations (
                operation_id TEXT, execution_epoch INTEGER, state TEXT, claim_sha256 BLOB, preparation_sha256 BLOB, receipt BLOB
            );
            """
        )
        operations = ("first-registration", "relocation")
        for revision, operation in enumerate(operations, 1):
            database.execute("INSERT INTO control_target_registrations VALUES (?,?,?,?,?)",
                             (operation, TARGET_ID, revision, revision, operation.encode()))
            database.execute("INSERT INTO control_operator_mutations VALUES (?,?,?,?)", (operation, operation, 1, 4))
        database.execute("INSERT INTO control_target_registration_locations VALUES (?,?)", (operations[1], b"signed-location-example"))
        initialized = InitializedFilesystemTarget(b"body", b"reply", "initialization", tmp_path / "uninitialised",
                                                  "target_0123456789ab", "1" * 64)
        database.execute("INSERT INTO control_target_registrations VALUES (?,?,?,?,?)",
                         (initialized.operation, initialized.target_id, 1, 1, b"signed-initial-observation"))
        database.execute("INSERT INTO control_operator_mutations VALUES (?,?,?,?)",
                         (initialized.operation, initialized.operation, 1, 4))
        database.execute("INSERT INTO control_target_marker_operations VALUES (?,?,?,?,?,?)",
                         (initialized.operation, 1, "SETTLED", b"c" * 32, b"p" * 32, b"signed-marker-receipt"))
        database.commit()
        expected = {
            "registration": hashlib.sha256(operations[0].encode()).hexdigest(),
            "move": hashlib.sha256(operations[1].encode()).hexdigest(),
            "oldLocation": hashlib.sha256(b"signed-location-example").hexdigest(),
            "initialization": hashlib.sha256(b"signed-initial-observation").hexdigest(),
            "markerEffect": hashlib.sha256(b"signed-marker-receipt").hexdigest(),
        }
        assert target_witnesses(tmp_path, operations, 4, initialized) == expected
        with pytest.raises(RuntimeError, match="WINDOWS_TARGET_REGISTRATION_WITNESS_INVALID"):
            target_witnesses(tmp_path, operations, 5, initialized)
        database.execute("UPDATE control_target_marker_operations SET state='DISPATCHED', receipt=NULL")
        database.commit()
        with pytest.raises(RuntimeError, match="WINDOWS_TARGET_INITIALIZATION_WITNESS_INVALID"):
            target_witnesses(tmp_path, operations, 4, initialized)
        database.execute("UPDATE control_target_marker_operations SET state='SETTLED', receipt=?", (b"changed-marker-receipt",))
        database.commit()
        changed = target_witnesses(tmp_path, operations, 4, initialized)
        assert changed == dict(expected, markerEffect=hashlib.sha256(b"changed-marker-receipt").hexdigest())
        database.execute("UPDATE control_target_registration_locations SET operation_id=?", (operations[0],))
        database.commit()
        with pytest.raises(RuntimeError, match="WINDOWS_TARGET_REGISTRATION_WITNESS_INVALID"):
            target_witnesses(tmp_path, operations, 4, initialized)


def source_tree(tmp_path: Path) -> tuple[Path, str]:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], capture_output=True, check=True)
    subprocess.run(["git", "config", "--local", "core.autocrlf", "false"], cwd=tmp_path, capture_output=True, check=True)
    path = tmp_path / "go/fixture.go"
    path.parent.mkdir()
    path.write_bytes(b"package fixture\n")
    subprocess.run(["git", "add", "--", "go/fixture.go"], cwd=tmp_path, capture_output=True, check=True)
    tree = subprocess.check_output(["git", "write-tree"], cwd=tmp_path).decode().strip()
    return path, tree


def test_platform_source_gate_checks_actual_committed_bytes(tmp_path: Path) -> None:
    _, tree = source_tree(tmp_path)
    observed = canonical_source(tmp_path, tree)
    assert observed["sourceFilesVerified"] == observed["nativeInputsVerified"] == 1


def test_platform_source_gate_refuses_checkout_newline_conversion(tmp_path: Path) -> None:
    path, tree = source_tree(tmp_path)
    path.write_bytes(b"package fixture\r\n")
    with pytest.raises(RuntimeError, match="WINDOWS_SOURCE_BYTES_DIFFER_FROM_COMMIT"):
        canonical_source(tmp_path, tree)


def test_windows_job_environment_uses_contexts_available_before_runner_assignment() -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/native-windows-cloud.yml"
    environment = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["windows-app"]["env"]
    allowed = {"github", "needs", "strategy", "matrix", "vars", "secrets", "inputs"}
    for name, value in environment.items():
        references = set(re.findall(r"\$\{\{\s*([a-zA-Z_][a-zA-Z_0-9]*)\.", str(value)))
        assert references <= allowed, f"{name} uses a context unavailable in jobs.env: {sorted(references - allowed)}"
