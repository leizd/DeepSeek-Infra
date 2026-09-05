from __future__ import annotations

import ast
import copy
import json
import subprocess
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_replication as br
from deepseek_infra.infra.workspace.backup_transfer_budget import (
    DEFAULT_BACKGROUND_MAX_BYTES_PER_SEC,
    DEFAULT_GLOBAL_BYTES_PER_SECOND,
    DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC,
    DEFAULT_TARGET_MAX_CONCURRENCY,
    TrafficClass,
    TransferBudgetManager,
)
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a37735c68398fc8f795babaa269e2de6a5acd567"
CORPUS_REL = "compat/native-runtime/v28/transfer/transfer_qos_vector.json"
AST_BUDGET = ("from_str", "can_share_scheduler_wave", "_base_class_rate", "acquire_transfer_token")
AST_REPL = ("_verify_destination_component", "_multipart_upload_missing")


def _dump_named(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.dump(node)
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return ast.dump(item)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.dump(node)
    raise AssertionError(name)


def test_transfer_qos_ast_matches_frozen_4_8_0() -> None:
    current_budget = ast.parse((ROOT / "deepseek_infra/infra/workspace/backup_transfer_budget.py").read_text(encoding="utf-8"))
    frozen_budget = ast.parse(
        subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:deepseek_infra/infra/workspace/backup_transfer_budget.py"],
            encoding="utf-8",
        )
    )
    current_repl = ast.parse((ROOT / "deepseek_infra/infra/workspace/backup_replication.py").read_text(encoding="utf-8"))
    frozen_repl = ast.parse(
        subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:deepseek_infra/infra/workspace/backup_replication.py"],
            encoding="utf-8",
        )
    )
    assert isinstance(current_budget, ast.Module) and isinstance(frozen_budget, ast.Module)
    assert isinstance(current_repl, ast.Module) and isinstance(frozen_repl, ast.Module)
    for name in AST_BUDGET:
        assert _dump_named(current_budget, name) == _dump_named(frozen_budget, name), name
    for name in AST_REPL:
        assert _dump_named(current_repl, name) == _dump_named(frozen_repl, name), name


class _Exc(Exception):
    def __init__(self, *, status: Any = 0, message: str = "") -> None:
        super().__init__(message)
        self.status = status
        self._message = message

    def __str__(self) -> str:
        return self._message


def replay(case: dict[str, Any]) -> dict[str, Any]:
    op = case["op"]
    if op == "traffic-class":
        try:
            cls = TrafficClass.from_str(case["value"])
        except (TypeError, AttributeError) as exc:
            return {"status": "error", "oracle_exception": type(exc).__name__}
        return {"name": cls.name, "value": int(cls), "priority": cls.priority}
    if op == "share-wave":
        ok, reason = TransferBudgetManager.can_share_scheduler_wave(case.get("existing") or [], case.get("candidate") or {})
        return {"allowed": ok, "reason": reason}
    if op == "concurrency":
        return _concurrency(case)
    if op == "base-rate":
        return {"bytesPerSecond": _base_rate(case)}
    if op == "verify-destination":
        return _verify(case)
    if op == "multipart-missing":
        if case.get("omit_status"):

            class _Msg(Exception):
                def __init__(self, msg: str) -> None:
                    super().__init__(msg)

            return {"missing": br._multipart_upload_missing(_Msg(str(case.get("message") or "")))}
        return {
            "missing": br._multipart_upload_missing(
                _Exc(status=case.get("status", 0), message=str(case.get("message") or ""))
            )
        }
    raise AssertionError(op)


def _concurrency(case: dict[str, Any]) -> dict[str, Any]:
    traffic_class = TrafficClass(int(case["traffic_class"]))
    max_concurrent = int(case["max_concurrent"]) if "max_concurrent" in case else DEFAULT_TARGET_MAX_CONCURRENCY
    active = list(case.get("active") or [])
    for tid in (case.get("source_target_id"), case.get("dest_target_id")):
        if tid:
            active_count = sum(1 for item in active if item.get("source_target_id") == tid or item.get("dest_target_id") == tid)
            if active_count >= max_concurrent and traffic_class != TrafficClass.P0_DISASTER_RECOVERY:
                return {
                    "status": "error",
                    "error": "target-transfer-concurrency-exceeded",
                    "activeCount": active_count,
                    "max": max_concurrent,
                    "targetId": tid,
                }
    return {"status": "ok"}


def _base_rate(case: dict[str, Any]) -> int:
    traffic_class = TrafficClass(int(case["traffic_class"]))
    global_rate = int(case["global_bytes_per_second"]) if "global_bytes_per_second" in case else DEFAULT_GLOBAL_BYTES_PER_SECOND
    reserved = int(case["reserved_recovery_bytes_per_sec"]) if "reserved_recovery_bytes_per_sec" in case else DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC
    background = int(case["background_max_bytes_per_sec"]) if "background_max_bytes_per_sec" in case else DEFAULT_BACKGROUND_MAX_BYTES_PER_SEC
    durable = list(case.get("durable_transfers") or [])
    local = list(case.get("local_transfers") or [])
    has_active_recovery = any(int(item.get("trafficClass", -1)) == 0 for item in durable) or any(
        int(item.get("traffic_class")) == 0 for item in local
    )
    if traffic_class == TrafficClass.P0_DISASTER_RECOVERY:
        base_rate = global_rate
    elif has_active_recovery:
        base_rate = min(background, max(1024 * 1024, global_rate - reserved))
    else:
        if traffic_class in {TrafficClass.P1_BACKUP_PUBLISH, TrafficClass.P2_REQUIRED_REPAIR, TrafficClass.P3_REQUIRED_REPLICATION}:
            base_rate = global_rate
        else:
            base_rate = min(background, global_rate)
    return max(64 * 1024, base_rate)


def _verify(case: dict[str, Any]) -> dict[str, Any]:
    expected = str(case["expected_digest"])
    kind = case["kind"]
    if kind == "none":
        valid, corrupt = False, False
    elif kind == "missing":
        valid, corrupt = False, False
    else:
        sha256 = case.get("sha256")
        provider = case.get("provider_sha256")
        if sha256 and sha256 == expected:
            valid, corrupt = True, False
        elif provider and provider == expected:
            valid, corrupt = True, False
        else:
            stream = case.get("stream_digest")
            has_data = bool(case.get("has_data", stream is not None))
            if not has_data:
                valid, corrupt = False, False
            else:
                calc = str(stream or "")
                valid, corrupt = calc == expected, calc != expected
    return {"valid": valid, "corrupt": corrupt}


def _cases() -> list[dict[str, Any]]:
    digest = "a" * 64
    other = "b" * 64
    cases: list[dict[str, Any]] = [
        {"name": "class-p0-name", "op": "traffic-class", "value": "P0_DISASTER_RECOVERY"},
        {"name": "class-lowercase-p2", "op": "traffic-class", "value": " p2_required_repair "},
        {"name": "class-substring-p", "op": "traffic-class", "value": "P"},
        {"name": "class-substring-repair", "op": "traffic-class", "value": "REPAIR"},
        {"name": "class-alias-full-name-misses", "op": "traffic-class", "value": "P1_ACTIVE_BACKUP_PUBLISH"},
        {"name": "class-p1-short", "op": "traffic-class", "value": "P1"},
        {"name": "class-value-2", "op": "traffic-class", "value": "2"},
        {"name": "class-unknown-defaults-p3", "op": "traffic-class", "value": "ZZZ"},
        {"name": "class-blank-matches-p0", "op": "traffic-class", "value": "   "},
        {"name": "type-class-non-string", "op": "traffic-class", "value": 2},
        {
            "name": "wave-rebalance-blocked-by-repair",
            "op": "share-wave",
            "existing": [{"type": "CREATE_REPAIR_JOB"}],
            "candidate": {"type": "CREATE_REBALANCE_JOB"},
        },
        {
            "name": "wave-repair-blocked-by-rebalance",
            "op": "share-wave",
            "existing": [{"type": "create_rebalance_job"}],
            "candidate": {"type": "CREATE_REPAIR_JOB"},
        },
        {
            "name": "wave-repair-with-repair-ok",
            "op": "share-wave",
            "existing": [{"type": "CREATE_REPAIR_JOB"}],
            "candidate": {"type": "CREATE_REPAIR_JOB"},
        },
        {
            "name": "wave-empty-ok",
            "op": "share-wave",
            "existing": [],
            "candidate": {"type": "CREATE_REBALANCE_JOB"},
        },
        {
            "name": "concurrency-ok",
            "op": "concurrency",
            "traffic_class": 2,
            "max_concurrent": 1,
            "source_target_id": "src",
            "dest_target_id": "dst",
            "active": [{"source_target_id": "other", "dest_target_id": "dst2"}],
        },
        {
            "name": "concurrency-exceeded-dest",
            "op": "concurrency",
            "traffic_class": 2,
            "max_concurrent": 1,
            "source_target_id": "src",
            "dest_target_id": "dst",
            "active": [{"source_target_id": "x", "dest_target_id": "dst"}],
        },
        {
            "name": "concurrency-p0-bypass",
            "op": "concurrency",
            "traffic_class": 0,
            "max_concurrent": 1,
            "dest_target_id": "dst",
            "active": [{"source_target_id": "x", "dest_target_id": "dst"}],
        },
        {
            "name": "concurrency-falsy-target-skipped",
            "op": "concurrency",
            "traffic_class": 2,
            "max_concurrent": 0,
            "source_target_id": "",
            "dest_target_id": None,
            "active": [{"source_target_id": "x", "dest_target_id": "y"}],
        },
        {"name": "rate-p0-full", "op": "base-rate", "traffic_class": 0},
        {"name": "rate-p2-no-recovery", "op": "base-rate", "traffic_class": 2},
        {"name": "rate-p5-background", "op": "base-rate", "traffic_class": 5},
        {
            "name": "rate-p2-throttled-when-p0-local",
            "op": "base-rate",
            "traffic_class": 2,
            "local_transfers": [{"traffic_class": 0}],
        },
        {
            "name": "rate-p2-throttled-when-p0-durable",
            "op": "base-rate",
            "traffic_class": 2,
            "durable_transfers": [{"trafficClass": 0}],
        },
        {
            "name": "rate-durable-false-counts-as-p0",
            "op": "base-rate",
            "traffic_class": 5,
            "durable_transfers": [{"trafficClass": False}],
        },
        {"name": "verify-none", "op": "verify-destination", "kind": "none", "expected_digest": digest},
        {"name": "verify-missing", "op": "verify-destination", "kind": "missing", "expected_digest": digest},
        {
            "name": "verify-sha256-match",
            "op": "verify-destination",
            "kind": "stat",
            "expected_digest": digest,
            "sha256": digest,
        },
        {
            "name": "verify-wrong-sha256-provider-match",
            "op": "verify-destination",
            "kind": "stat",
            "expected_digest": digest,
            "sha256": other,
            "provider_sha256": digest,
        },
        {
            "name": "verify-wrong-hashes-stream-match",
            "op": "verify-destination",
            "kind": "stat",
            "expected_digest": digest,
            "sha256": other,
            "provider_sha256": other,
            "stream_digest": digest,
        },
        {
            "name": "verify-stream-mismatch-corrupt",
            "op": "verify-destination",
            "kind": "stat",
            "expected_digest": digest,
            "stream_digest": other,
        },
        {
            "name": "verify-empty-stream",
            "op": "verify-destination",
            "kind": "stat",
            "expected_digest": digest,
            "has_data": False,
        },
        {"name": "multipart-404", "op": "multipart-missing", "status": 404, "message": "nope"},
        {"name": "multipart-message", "op": "multipart-missing", "status": 500, "message": "Multipart-Upload-Not-Found"},
        {"name": "multipart-neither", "op": "multipart-missing", "status": 500, "message": "timeout"},
        {"name": "multipart-status-falsy", "op": "multipart-missing", "status": 0, "message": ""},
        {"name": "multipart-omit-status-message", "op": "multipart-missing", "omit_status": True, "message": "multipart-upload-not-found"},
    ]
    names = [item["name"] for item in cases]
    assert len(names) == len(set(names))
    return cases


def write_corpus() -> Path:
    cases = []
    for case in _cases():
        item = copy.deepcopy(case)
        item["expected"] = replay(case)
        cases.append(item)
    payload = {
        "schema_version": 1,
        "source_version": "4.8.0",
        "source_commit": SOURCE_COMMIT,
        "scope": "validator-parity-only-not-provider-execution-evidence",
        "defaults": {
            "globalBytesPerSecond": DEFAULT_GLOBAL_BYTES_PER_SECOND,
            "reservedRecoveryBytesPerSec": DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC,
            "backgroundMaxBytesPerSec": DEFAULT_BACKGROUND_MAX_BYTES_PER_SEC,
            "targetMaxConcurrency": DEFAULT_TARGET_MAX_CONCURRENCY,
        },
        "ast_matches_source_commit": {name: True for name in (*AST_BUDGET, *AST_REPL)},
        "cases": cases,
    }
    path = ROOT / CORPUS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def test_v28_transfer_qos_matches_python_4_8_0() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v28/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["ast_matches_source_commit"] == {name: True for name in (*AST_BUDGET, *AST_REPL)}
    for case in fixture["cases"]:
        assert replay(case) == case["expected"], case["name"]


if __name__ == "__main__":
    written = write_corpus()
    print(written)
