from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_native_macos_product as fixture
import verify_native_macos_product as probe


@pytest.mark.parametrize("platform,environment", [
    ("win32", {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}),
    ("darwin", {}), ("darwin", {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push"}),
])
def test_product_probe_refuses_the_local_machine_and_non_dispatch_contexts(platform: str, environment: dict[str, str]) -> None:
    with pytest.raises(RuntimeError, match="MACOS_PRODUCT_REQUIRES_DISPOSABLE_CLOUD"):
        probe.require_cloud(environment, platform)


def test_product_probe_accepts_only_the_explicit_cloud_mac_context() -> None:
    probe.require_cloud({"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}, "darwin")


def bsd_record(pid: int = 901, seconds: int = 100, microseconds: int = 234567) -> bytes:
    # Apple's public proc_bsdinfo ABI: 12 uint32, 16/32-byte names, 6 uint32, 2 uint64.
    return struct.pack("=12I16s32s6I2Q", 0, 2, 0, pid, 800, 501, 20, 501, 20, 501, 20, 0,
                       b"deepseekd", b"deepseekd", 4, 901, 0, 0, 0, 0, seconds, microseconds)


def test_native_process_identity_keeps_microsecond_precision_and_real_parent_uid() -> None:
    value = probe.decode_bsdinfo(bsd_record(), 901)
    assert value == dict(pid=901, parentPid=800, uid=501, status=2, startMicroseconds=100234567)
    assert probe.decode_bsdinfo(bsd_record(microseconds=234568), 901)["startMicroseconds"] != value["startMicroseconds"]


@pytest.mark.parametrize("raw,pid", [(b"short", 901), (bsd_record(pid=902), 901),
                                     (bsd_record(seconds=0), 901), (bsd_record(microseconds=1_000_000), 901)])
def test_process_identity_refuses_wrong_abi_pid_or_clock(raw: bytes, pid: int) -> None:
    with pytest.raises(RuntimeError, match="MACOS_PROCESS_IDENTITY_INVALID"):
        probe.decode_bsdinfo(raw, pid)


def test_owned_tree_uses_ancestry_and_does_not_include_an_unrelated_same_name() -> None:
    rows: list[dict[str, Any]] = [dict(pid=50, parentPid=9), dict(pid=51, parentPid=50),
        dict(pid=52, parentPid=51), dict(pid=99, parentPid=8)]
    assert {r["pid"] for r in probe.owned_descendants(rows, 50)} == {50, 51, 52}
    assert probe.owned_descendants(rows, 999) == []


@pytest.mark.parametrize("change", ["scheduler-state", "start-time", "parent", "exited", "zombie"])
def test_native_tree_accepts_scheduler_changes_but_refuses_reused_or_reparented_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    executable = tmp_path / "native-fixture"
    executable.write_bytes(b"exact observed executable fixture")
    before = probe.decode_bsdinfo(bsd_record(), 901)
    changed = dict(before)
    if change == "zombie":
        changed["status"] = 5
    elif change != "exited":
        changed[{"scheduler-state": "status", "start-time": "startMicroseconds", "parent": "parentPid"}[change]] += 1
    reads = iter([before, None if change == "exited" else changed])
    native = object.__new__(probe.NativeProcesses)
    monkeypatch.setattr(native, "identity", lambda _pid: next(reads))
    monkeypatch.setattr(probe.subprocess, "check_output", lambda *args, **kwargs: "901 800\n")

    def pidpath(pid: int, buffer: Any, size: int) -> int:
        buffer.value = str(executable).encode()
        return len(buffer.value)

    setattr(native, "library", SimpleNamespace(proc_pidpath=pidpath))
    if change == "scheduler-state":
        rows = native.tree(901)
        assert len(rows) == 1 and rows[0]["startMicroseconds"] == before["startMicroseconds"]
        assert rows[0]["executableSha256"] == hashlib.sha256(executable.read_bytes()).hexdigest()
    elif change in {"exited", "zombie"}:
        assert native.tree(901) == []
    else:
        with pytest.raises(RuntimeError, match="MACOS_PROCESS_IDENTITY_CHANGED_DURING_OBSERVATION"):
            native.tree(901)


def fixture_tree(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = []
    for arch, cpu in [("amd64", 0x01000007), ("arm64", 0x0100000C)]:
        for role in ["deepseekd", "deepseek-launch", "deepseek-desktop"]:
            path = root / arch / role
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(struct.pack("<8I", 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0) + b"native fixture")
            rows.append(dict(path=path.relative_to(root).as_posix(), bytes=path.stat().st_size,
                             sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    for name in ["static/ui/index.html", "static/ui/.vite/manifest.json"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("controlled static fixture", encoding="utf-8")
        rows.append(dict(path=name, bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    pin = dict(version="1.27.1", artifacts=dict(linux_amd64=dict(sha256="a" * 64)))
    report = dict(sourceCommit="b" * 40, sourceTree="c" * 40, runId="12", attempt="1",
                  goVersion="go version go1.27.1 linux/amd64", linuxGoArchiveSha256="a" * 64,
                  cgoEnabled=False, files=rows)
    (root / "product-fixture.json").write_text(json.dumps(report), encoding="utf-8")
    return report, pin


def test_fixture_keeps_both_architectures_real_header_bindings_and_hidden_vite_manifest(tmp_path: Path) -> None:
    report, pin = fixture_tree(tmp_path)
    assert fixture.verify_fixture(tmp_path, report["sourceCommit"], report["sourceTree"], "12", "1", pin) == report


@pytest.mark.parametrize("change", ["hidden-missing", "changed-bytes", "extra-file", "wrong-cpu", "wrong-source", "wrong-attempt"])
def test_fixture_refuses_missing_hidden_assets_hash_drift_extra_files_or_wrong_source(tmp_path: Path, change: str) -> None:
    report, pin = fixture_tree(tmp_path)
    if change == "hidden-missing":
        (tmp_path / "static/ui/.vite/manifest.json").unlink()
    elif change == "changed-bytes":
        (tmp_path / "static/ui/index.html").write_text("changed", encoding="utf-8")
    elif change == "extra-file":
        (tmp_path / "unexpected").write_bytes(b"not in receipt")
    elif change == "wrong-cpu":
        path = tmp_path / "arm64/deepseekd"
        path.write_bytes(struct.pack("<8I", 0xFEEDFACF, 0x01000007, 0, 2, 0, 0, 0, 0) + b"native fixture")
        row = next(r for r in report["files"] if r["path"] == "arm64/deepseekd")
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif change == "wrong-source":
        report["sourceCommit"] = "d" * 40
    else:
        report["attempt"] = "2"
    (tmp_path / "product-fixture.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(RuntimeError, match="MACOS_PRODUCT_FIXTURE"):
        fixture.verify_fixture(tmp_path, "b" * 40, "c" * 40, "12", "1", pin)


def test_product_workflow_keeps_original_windows_job_and_both_existing_window_limits() -> None:
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/native-windows-cloud.yml").read_text())
    windows_job = workflow["jobs"]["windows-app"]
    added = [step for step in windows_job["steps"]
             if step.get("name") == "Verify streamed media images records compact results and digest corpus on Windows"]
    assert len(added) == 1
    # Bind the approved additional gate separately; preserve the original job's frozen hash.
    extra = json.dumps(added[0], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(extra).hexdigest() == "ee4475c1610cd1079cc79193157e6e2d13fb91bb737ec789911dcfb85b20e4fe"  # pragma: allowlist secret
    original_job = dict(windows_job, steps=[step for step in windows_job["steps"] if step is not added[0]])
    original = json.dumps(original_job, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(original).hexdigest() == "8108d2be47dea2b3ee06cd760950a5d338fecea0f2e68a881106f7870f4ac537"
    assert workflow["jobs"]["macos-window"]["timeout-minutes"] == 15
    job = workflow["jobs"]["macos-product"]
    assert job["needs"] == "macos-build" and job["timeout-minutes"] == 60
    assert job["if"] == "github.event_name == 'workflow_dispatch' && inputs.verify_macos_product"
    assert {(r["architecture"], r["runner"]) for r in job["strategy"]["matrix"]["include"]} == {
        ("amd64", "macos-15-intel"), ("arm64", "macos-15"),
    }
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "rust_build.py --toolchain 1.85.0" in commands
    assert "verify_native_macos_product.py" in commands and "prepare_native_macos_product.py" in commands
    assert all(step.get("continue-on-error", False) is False for step in job["steps"])
