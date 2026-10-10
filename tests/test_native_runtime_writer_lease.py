from __future__ import annotations

import ast
import copy
import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_writer_lease as wl
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a37735c68398fc8f795babaa269e2de6a5acd567"
NOW = "2026-09-05T15:00:00Z"
CORPUS_REL = "compat/native-runtime/v25/transfer/writer_lease_vector.json"
AST_NAMES = (
    "TARGET_WRITER_LEASE_SECONDS",
    "CLOCK_SKEW_SAFETY_SECONDS",
    "active_writer_lease",
    "_payload",
    "_expired",
    "_same_run_takeover_allowed",
    "_assert_payload_owned",
    "assert_owned",
    "acquire",
)


def _dump_named(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.dump(node)
        if isinstance(node, ast.ClassDef):
            if node.name == name:
                return ast.dump(node)
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return ast.dump(item)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.dump(node)
    raise AssertionError(name)


def test_writer_lease_helpers_ast_match_frozen_4_8_0() -> None:
    current = ast.parse((ROOT / "deepseek_infra/infra/workspace/backup_writer_lease.py").read_text(encoding="utf-8"))
    frozen = ast.parse(
        subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:deepseek_infra/infra/workspace/backup_writer_lease.py"],
            encoding="utf-8",
        )
    )
    assert isinstance(current, ast.Module)
    assert isinstance(frozen, ast.Module)
    for name in AST_NAMES:
        assert _dump_named(current, name) == _dump_named(frozen, name), name


def replay(case: dict[str, Any], now: str = NOW) -> dict[str, Any]:
    op = case["op"]
    if op == "payload":
        return {"payload": _payload(case, now)}
    if op == "expired":
        return {"expired": _expired(case["existing"], now)}
    if op == "active":
        return {"active": _active(case.get("payload"), now)}
    if op == "same-run-takeover":
        try:
            return {"allowed": _same_run_takeover(case["existing"], case["owner_run_id"], case["fencing_token"])}
        except (TypeError, ValueError) as exc:
            return {"status": "error", "oracle_exception": type(exc).__name__}
    if op == "acquire":
        return _acquire(case, now)
    if op == "assert-owned":
        return _assert_owned(case, now)
    if op == "release-allowed":
        return _release_allowed(case)
    raise AssertionError(op)


def _now_dt(now: str):
    return wl.datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(wl.timezone.utc)


def _payload(case: dict[str, Any], now: str) -> dict[str, Any]:
    acquired = _now_dt(now)
    lease_seconds = int(case["lease_seconds"]) if "lease_seconds" in case else wl.TARGET_WRITER_LEASE_SECONDS
    return {
        "schemaVersion": 1,
        "targetId": case.get("target_id"),
        "ownerRunId": case.get("owner_run_id"),
        "ownerInstanceId": case.get("owner_instance_id"),
        "fencingToken": case.get("fencing_token"),
        "acquiredAt": wl._utc_iso(acquired),
        "expiresAt": wl._utc_iso(acquired + timedelta(seconds=lease_seconds)),
    }


def _expired(existing: dict[str, Any], now: str) -> bool:
    return str(existing.get("expiresAt") or "") < wl._utc_iso(_now_dt(now))


def _active(payload: Any, now: str) -> bool:
    if payload is None:
        return True
    if not isinstance(payload, dict):
        return True
    expires_at = payload.get("expiresAt")
    if not isinstance(expires_at, str):
        return True
    try:
        expiry = wl.datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(wl.timezone.utc)
    except (TypeError, ValueError):
        return True
    return expiry > _now_dt(now)


def _same_run_takeover(existing: dict[str, Any], owner_run_id: Any, fencing_token: Any) -> bool:
    return str(existing.get("ownerRunId") or "") == owner_run_id and int(existing.get("fencingToken") or -1) < int(fencing_token)


def _acquire(case: dict[str, Any], now: str) -> dict[str, Any]:
    existing = case.get("existing")
    owner_run_id = case["owner_run_id"]
    fencing_token = int(case["fencing_token"])
    try:
        if existing is None:
            return {"decision": "create", "payload": _payload(case, now)}
        same_run = _same_run_takeover(existing, owner_run_id, fencing_token)
        if not _expired(existing, now) and not same_run:
            return {"decision": "busy", "error": "Target writer is busy with another run"}
        if int(existing.get("fencingToken") or 0) >= fencing_token:
            return {"decision": "newer-token", "error": "Target writer is held by a newer or equal fencing token"}
        return {"decision": "preempt", "payload": _payload(case, now)}
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}


def _assert_owned(case: dict[str, Any], now: str) -> dict[str, Any]:
    existing = case.get("existing")
    if existing is None:
        return {"status": "error", "error": "missing"}
    try:
        if (
            str(existing.get("ownerRunId") or "") != case["owner_run_id"]
            or str(existing.get("ownerInstanceId") or "") != case["owner_instance_id"]
            or int(existing.get("fencingToken") or -1) != int(case["fencing_token"])
        ):
            return {"status": "error", "error": "stolen"}
        if _expired(existing, now):
            return {"status": "error", "error": "expired"}
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}
    return {"status": "ok"}


def _release_allowed(case: dict[str, Any]) -> dict[str, Any]:
    existing = case.get("existing")
    if existing is None:
        return {"delete": False}
    try:
        allowed = (
            str(existing.get("ownerRunId") or "") == case["owner_run_id"]
            and int(existing.get("fencingToken") or -1) == int(case["fencing_token"])
        )
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}
    return {"delete": allowed}


def _lease(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemaVersion": 1,
        "targetId": "target-a",
        "ownerRunId": "run-frozen-1",
        "ownerInstanceId": "worker-a",
        "fencingToken": 1,
        "acquiredAt": NOW,
        "expiresAt": "2026-09-05T15:05:00Z",
    }
    body.update(overrides)
    return body


def _identity(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "target_id": "target-a",
        "owner_run_id": "run-frozen-1",
        "owner_instance_id": "worker-a",
        "fencing_token": 2,
    }
    body.update(overrides)
    return body


def _cases() -> list[dict[str, Any]]:
    ident = _identity()
    cases: list[dict[str, Any]] = [
        {"name": "payload-default-300s", "op": "payload", **ident, "fencing_token": 2},
        {"name": "payload-custom-lease", "op": "payload", **ident, "lease_seconds": 60},
        {"name": "expired-past", "op": "expired", "existing": _lease(expiresAt="2026-09-05T14:59:59Z")},
        {"name": "expired-equal-now-not-expired", "op": "expired", "existing": _lease(expiresAt=NOW)},
        {"name": "expired-future", "op": "expired", "existing": _lease(expiresAt="2026-09-05T15:00:01Z")},
        {"name": "expired-missing-falsy", "op": "expired", "existing": _lease(expiresAt=None)},
        {"name": "expired-false-falsy", "op": "expired", "existing": _lease(expiresAt=False)},
        {"name": "expired-true-string-compare", "op": "expired", "existing": _lease(expiresAt=True)},
        {"name": "expired-malformed-zzz", "op": "expired", "existing": _lease(expiresAt="zzz")},
        {"name": "active-missing-payload-fail-closed", "op": "active", "payload": None},
        {"name": "active-non-object-fail-closed", "op": "active", "payload": ["x"]},
        {"name": "active-non-string-expiry-fail-closed", "op": "active", "payload": _lease(expiresAt=1)},
        {"name": "active-malformed-fail-closed", "op": "active", "payload": _lease(expiresAt="zzz")},
        {"name": "active-past", "op": "active", "payload": _lease(expiresAt="2026-09-05T14:59:59Z")},
        {"name": "active-equal-now-not-active", "op": "active", "payload": _lease(expiresAt=NOW)},
        {"name": "active-future", "op": "active", "payload": _lease(expiresAt="2026-09-05T15:00:01Z")},
        {
            "name": "takeover-same-run-higher-token",
            "op": "same-run-takeover",
            "existing": _lease(fencingToken=1),
            "owner_run_id": "run-frozen-1",
            "fencing_token": 2,
        },
        {
            "name": "takeover-same-run-equal-token",
            "op": "same-run-takeover",
            "existing": _lease(fencingToken=2),
            "owner_run_id": "run-frozen-1",
            "fencing_token": 2,
        },
        {
            "name": "takeover-other-run",
            "op": "same-run-takeover",
            "existing": _lease(ownerRunId="run-other", fencingToken=1),
            "owner_run_id": "run-frozen-1",
            "fencing_token": 2,
        },
        {
            "name": "takeover-missing-token-uses-minus-one",
            "op": "same-run-takeover",
            "existing": _lease(fencingToken=None),
            "owner_run_id": "run-frozen-1",
            "fencing_token": 1,
        },
        {"name": "acquire-missing-creates", "op": "acquire", "existing": None, **ident},
        {
            "name": "acquire-busy-other-unexpired",
            "op": "acquire",
            "existing": _lease(ownerRunId="run-other", expiresAt="2026-09-05T15:05:00Z"),
            **ident,
        },
        {
            "name": "acquire-preempt-expired-lower-token",
            "op": "acquire",
            "existing": _lease(ownerRunId="run-other", fencingToken=1, expiresAt="2026-09-05T14:59:59Z"),
            **ident,
        },
        {
            "name": "acquire-newer-token-even-if-expired",
            "op": "acquire",
            "existing": _lease(ownerRunId="run-other", fencingToken=3, expiresAt="2026-09-05T14:59:59Z"),
            **ident,
        },
        {
            "name": "acquire-same-run-takeover-unexpired",
            "op": "acquire",
            "existing": _lease(fencingToken=1, expiresAt="2026-09-05T15:05:00Z"),
            **ident,
        },
        {
            "name": "acquire-same-run-equal-token-busy",
            "op": "acquire",
            "existing": _lease(fencingToken=2, expiresAt="2026-09-05T15:05:00Z"),
            **ident,
        },
        {
            "name": "acquire-missing-token-or-zero-vs-minus-one",
            "op": "acquire",
            "existing": _lease(fencingToken=None, expiresAt="2026-09-05T14:59:59Z"),
            **_identity(fencing_token=0),
        },
        {
            "name": "type-acquire-token-object",
            "op": "acquire",
            "existing": _lease(fencingToken={"n": 1}, expiresAt="2026-09-05T14:59:59Z"),
            **ident,
        },
        {"name": "assert-ok", "op": "assert-owned", "existing": _lease(), **_identity(fencing_token=1)},
        {"name": "assert-missing", "op": "assert-owned", "existing": None, **_identity(fencing_token=1)},
        {
            "name": "assert-stolen-run",
            "op": "assert-owned",
            "existing": _lease(ownerRunId="run-other"),
            **_identity(fencing_token=1),
        },
        {
            "name": "assert-stolen-instance",
            "op": "assert-owned",
            "existing": _lease(ownerInstanceId="worker-b"),
            **_identity(fencing_token=1),
        },
        {
            "name": "assert-stolen-token",
            "op": "assert-owned",
            "existing": _lease(fencingToken=9),
            **_identity(fencing_token=1),
        },
        {
            "name": "assert-expired",
            "op": "assert-owned",
            "existing": _lease(expiresAt="2026-09-05T14:59:59Z"),
            **_identity(fencing_token=1),
        },
        {
            "name": "assert-equal-now-ok",
            "op": "assert-owned",
            "existing": _lease(expiresAt=NOW),
            **_identity(fencing_token=1),
        },
        {"name": "release-owner-match", "op": "release-allowed", "existing": _lease(), **_identity(fencing_token=1)},
        {
            "name": "release-token-mismatch",
            "op": "release-allowed",
            "existing": _lease(fencingToken=9),
            **_identity(fencing_token=1),
        },
        {"name": "release-missing", "op": "release-allowed", "existing": None, **_identity(fencing_token=1)},
        {
            "name": "release-does-not-require-instance",
            "op": "release-allowed",
            "existing": _lease(ownerInstanceId="other-worker"),
            **_identity(fencing_token=1),
        },
        {
            "name": "type-same-run-token-array",
            "op": "same-run-takeover",
            "existing": _lease(fencingToken=[1]),
            "owner_run_id": "run-frozen-1",
            "fencing_token": 2,
        },
    ]
    names = [item["name"] for item in cases]
    assert len(names) == len(set(names))
    return cases


def write_corpus() -> Path:
    cases = []
    for case in _cases():
        item = copy.deepcopy(case)
        try:
            item["expected"] = replay(case)
        except (TypeError, ValueError) as exc:
            item["expected"] = {"status": "error", "oracle_exception": type(exc).__name__}
        cases.append(item)
    payload = {
        "schema_version": 1,
        "source_version": "4.8.0",
        "source_commit": SOURCE_COMMIT,
        "scope": "validator-parity-only-not-provider-execution-evidence",
        "now": NOW,
        "lease_seconds": wl.TARGET_WRITER_LEASE_SECONDS,
        "clock_skew_safety_seconds": wl.CLOCK_SKEW_SAFETY_SECONDS,
        "ast_matches_source_commit": {name: True for name in AST_NAMES},
        "cases": cases,
    }
    path = ROOT / CORPUS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def test_v25_writer_lease_matches_python_4_8_0_helpers() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v25/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["now"] == NOW
    assert fixture["lease_seconds"] == wl.TARGET_WRITER_LEASE_SECONDS
    assert fixture["clock_skew_safety_seconds"] == wl.CLOCK_SKEW_SAFETY_SECONDS
    assert fixture["ast_matches_source_commit"] == {name: True for name in AST_NAMES}
    for case in fixture["cases"]:
        try:
            result = replay(case, fixture["now"])
        except (TypeError, ValueError) as exc:
            result = {"status": "error", "oracle_exception": type(exc).__name__}
        assert result == case["expected"], case["name"]


if __name__ == "__main__":
    written = write_corpus()
    print(written)
