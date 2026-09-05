from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_publish as bp
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a37735c68398fc8f795babaa269e2de6a5acd567"
CORPUS_REL = "compat/native-runtime/v26/transfer/recovery_authenticate_vector.json"
POLICY = "policy-frozen-1"
BACKUP = "backup-frozen-1"
OSD = "a" * 64
PARENT = "backup-parent-1"
AST_NAMES = (
    "authenticate_recovery_copy",
    "authenticate_committed_copy",
    "authenticate_transition_parent",
)
PUBLISH_AST_NAMES = ("_stable_json", "_commit_hash")


def _dump_named(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.dump(node)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.dump(node)
    raise AssertionError(name)


def test_recovery_authenticate_ast_matches_frozen_4_8_0() -> None:
    current_rep = ast.parse((ROOT / "deepseek_infra/infra/workspace/backup_replication.py").read_text(encoding="utf-8"))
    frozen_rep = ast.parse(
        subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:deepseek_infra/infra/workspace/backup_replication.py"],
            encoding="utf-8",
        )
    )
    current_pub = ast.parse((ROOT / "deepseek_infra/infra/workspace/backup_publish.py").read_text(encoding="utf-8"))
    frozen_pub = ast.parse(
        subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:deepseek_infra/infra/workspace/backup_publish.py"],
            encoding="utf-8",
        )
    )
    assert isinstance(current_rep, ast.Module) and isinstance(frozen_rep, ast.Module)
    assert isinstance(current_pub, ast.Module) and isinstance(frozen_pub, ast.Module)
    for name in AST_NAMES:
        assert _dump_named(current_rep, name) == _dump_named(frozen_rep, name), name
    for name in PUBLISH_AST_NAMES:
        assert _dump_named(current_pub, name) == _dump_named(frozen_pub, name), name


def _encode(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _receipt(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemaVersion": 4,
        "policyId": POLICY,
        "backupId": BACKUP,
        "objectSetDigest": OSD,
        "controlObjectDigest": "b" * 64,
        "storageProtocol": "object-set-v1",
        "targetId": "target-a",
    }
    body.update(overrides)
    return body


def _commit_for(receipt_bytes: bytes, receipt: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemaVersion": 4,
        "policyId": POLICY,
        "backupId": BACKUP,
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "objectSetDigest": receipt.get("objectSetDigest") or receipt.get("objectDigest"),
        "previousCommitHash": "0" * 64,
        "targetGeneration": 1,
        "fencingToken": 1,
        "runId": "run-frozen-1",
        "scheduleSlot": "replica/backup-frozen-1",
        "storageProtocol": "object-set-v1",
        "lineageId": "lineage-frozen-1",
    }
    body.update(overrides)
    if "commitHash" not in overrides:
        body.pop("commitHash", None)
        body["commitHash"] = bp._commit_hash(body)
    return body


def _pair(**receipt_over: Any) -> tuple[bytes, bytes, dict[str, Any], dict[str, Any]]:
    receipt = _receipt(**receipt_over)
    raw_receipt = _encode(receipt)
    commit = _commit_for(raw_receipt, receipt)
    return raw_receipt, _encode(commit), receipt, commit


def replay(case: dict[str, Any]) -> dict[str, Any]:
    op = case["op"]
    if op == "authenticate":
        return _authenticate(case)
    if op == "authenticate-committed":
        return _authenticate_committed(case)
    if op == "authenticate-parent":
        return _authenticate_parent(case)
    if op == "commit-hash":
        return {"commitHash": bp._commit_hash(copy.deepcopy(case["commit"]))}
    raise AssertionError(op)


def _bytes_field(case: dict[str, Any], key: str) -> bytes | None:
    if key not in case or case[key] is None:
        return None
    return bytes.fromhex(case[key])


def _authenticate(case: dict[str, Any]) -> dict[str, Any]:
    raw_receipt = _bytes_field(case, "raw_receipt")
    raw_commit = _bytes_field(case, "raw_commit")
    policy_id = case["policy_id"]
    backup_id = case["backup_id"]
    expected = case.get("expected_object_set_digest")
    status, receipt, commit = _authenticate_bytes(raw_receipt, raw_commit, policy_id, backup_id, expected)
    return {"status": status, "receipt": receipt, "commit": commit}


def _authenticate_bytes(
    raw_receipt: bytes | None,
    raw_commit: bytes | None,
    policy_id: str,
    backup_id: str,
    expected_object_set_digest: Any,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    if raw_receipt is None and raw_commit is None:
        return "missing", None, None
    if raw_receipt is None or raw_commit is None:
        return "corrupt", None, None
    try:
        receipt = json.loads(raw_receipt.decode("utf-8"))
        commit = json.loads(raw_commit.decode("utf-8"))
    except Exception:
        return "corrupt", None, None
    if not isinstance(receipt, dict) or not isinstance(commit, dict):
        return "corrupt", None, None
    calc_receipt_digest = hashlib.sha256(raw_receipt).hexdigest()
    if str(commit.get("receiptDigest")) != calc_receipt_digest:
        return "corrupt", receipt, commit
    try:
        schema_ver = int(commit.get("schemaVersion") or 0)
    except (TypeError, ValueError):
        return "corrupt", receipt, commit
    if schema_ver not in {1, 2, 3, 4}:
        return "corrupt", receipt, commit
    if str(commit.get("policyId")) != policy_id or str(commit.get("backupId")) != backup_id:
        return "conflicting", receipt, commit
    if str(receipt.get("policyId")) != policy_id or str(receipt.get("backupId")) != backup_id:
        return "conflicting", receipt, commit
    r_osd = receipt.get("objectSetDigest") or receipt.get("objectDigest")
    c_osd = commit.get("objectSetDigest") or commit.get("objectDigest")
    if not r_osd:
        return "corrupt", receipt, commit
    if c_osd is not None and r_osd != c_osd:
        return "corrupt", receipt, commit
    if expected_object_set_digest is not None:
        if str(r_osd) != expected_object_set_digest:
            return "conflicting", receipt, commit
    return "authenticated", receipt, commit


def _authenticate_committed(case: dict[str, Any]) -> dict[str, Any]:
    base = _authenticate(case)
    if base["status"] != "authenticated" or base["commit"] is None or base["receipt"] is None:
        return base
    commit = base["commit"]
    receipt = base["receipt"]
    if commit.get("commitHash"):
        calc = bp._commit_hash(commit)
        if str(commit.get("commitHash")) != calc:
            return {"status": "corrupt", "receipt": receipt, "commit": commit}
    expected_prev = case.get("expected_previous_commit_hash")
    if expected_prev is not None and str(commit.get("previousCommitHash") or "") != expected_prev:
        return {"status": "conflicting", "receipt": receipt, "commit": commit}
    expected_gen = case.get("expected_target_generation")
    if expected_gen is not None:
        try:
            if int(commit.get("targetGeneration") or 0) != expected_gen:
                return {"status": "conflicting", "receipt": receipt, "commit": commit}
        except (TypeError, ValueError) as exc:
            return {"status": "error", "oracle_exception": type(exc).__name__, "receipt": receipt, "commit": commit}
    return {"status": "authenticated", "receipt": receipt, "commit": commit}


def _authenticate_parent(case: dict[str, Any]) -> dict[str, Any]:
    status, receipt, commit = _authenticate_bytes(
        _bytes_field(case, "raw_receipt"),
        _bytes_field(case, "raw_commit"),
        case["policy_id"],
        case["expected_parent_backup_id"],
        case.get("expected_object_set_digest"),
    )
    if status != "authenticated" or commit is None or receipt is None:
        return {"ok": False, "reason": f"parent-copy-status-{status}"}
    if case.get("expected_receipt_digest") is not None:
        if str(commit.get("receiptDigest") or "") != case["expected_receipt_digest"]:
            return {"ok": False, "reason": "parent-receipt-digest-mismatch"}
    if case.get("expected_commit_hash") is not None:
        if str(commit.get("commitHash") or "") != case["expected_commit_hash"]:
            return {"ok": False, "reason": "parent-commit-hash-mismatch"}
    if case.get("expected_lineage_id") is not None:
        c_lineage = commit.get("lineageId") or receipt.get("lineageId")
        if c_lineage and str(c_lineage) != case["expected_lineage_id"]:
            return {"ok": False, "reason": "parent-lineage-mismatch"}
    if case.get("expected_object_set_digest") is not None:
        c_osd = (
            commit.get("objectSetDigest")
            or receipt.get("objectSetDigest")
            or commit.get("objectDigest")
            or receipt.get("objectDigest")
        )
        if str(c_osd or "") != case["expected_object_set_digest"]:
            return {"ok": False, "reason": "parent-object-set-digest-mismatch"}
    return {"ok": True, "reason": "authenticated"}


def _hex(data: bytes | None) -> str | None:
    if data is None:
        return None
    return data.hex()


def _cases() -> list[dict[str, Any]]:
    raw_r, raw_c, receipt, commit = _pair()
    parent_receipt = _receipt(backupId=PARENT)
    parent_raw_r = _encode(parent_receipt)
    parent_commit = _commit_for(parent_raw_r, parent_receipt, backupId=PARENT)
    parent_raw_c = _encode(parent_commit)
    digest_mismatch_commit = dict(commit)
    digest_mismatch_commit["receiptDigest"] = "0" * 64
    schema_true_commit = dict(commit)
    schema_true_commit["schemaVersion"] = True
    schema_true_commit.pop("commitHash", None)
    schema_true_commit["commitHash"] = bp._commit_hash(schema_true_commit)
    object_digest_receipt = _receipt(objectSetDigest="", objectDigest=OSD)
    object_digest_raw_r = _encode(object_digest_receipt)
    object_digest_commit = _commit_for(object_digest_raw_r, object_digest_receipt)
    number_osd_receipt = _receipt(objectSetDigest=1)
    number_osd_raw = _encode(number_osd_receipt)
    number_osd_commit = {
        "schemaVersion": 4,
        "policyId": POLICY,
        "backupId": BACKUP,
        "receiptDigest": hashlib.sha256(number_osd_raw).hexdigest(),
        "objectSetDigest": "1",
    }
    cases: list[dict[str, Any]] = [
        {
            "name": "authenticate-happy",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(raw_c),
        },
        {
            "name": "authenticate-missing-both",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": None,
            "raw_commit": None,
        },
        {
            "name": "authenticate-receipt-only-corrupt",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": None,
        },
        {
            "name": "authenticate-commit-only-corrupt",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": None,
            "raw_commit": _hex(raw_c),
        },
        {
            "name": "authenticate-invalid-json",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": b"{not-json".hex(),
            "raw_commit": _hex(raw_c),
        },
        {
            "name": "authenticate-invalid-utf8",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": "ff",
            "raw_commit": _hex(raw_c),
        },
        {
            "name": "authenticate-non-object",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(_encode([receipt])),
            "raw_commit": _hex(raw_c),
        },
        {
            "name": "authenticate-receipt-digest-mismatch",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(_encode(digest_mismatch_commit)),
        },
        {
            "name": "authenticate-schema-zero-corrupt",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(_encode({**commit, "schemaVersion": 0})),
        },
        {
            "name": "authenticate-schema-true-allowed",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(_encode(schema_true_commit)),
        },
        {
            "name": "authenticate-schema-five-corrupt",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(_encode({**commit, "schemaVersion": 5})),
        },
        {
            "name": "authenticate-commit-policy-none-string",
            "op": "authenticate",
            "policy_id": "None",
            "backup_id": BACKUP,
            "raw_receipt": _hex(_encode(_receipt(policyId=None))),
            "raw_commit": _hex(
                _encode(_commit_for(_encode(_receipt(policyId=None)), _receipt(policyId=None), policyId=None))
            ),
        },
        {
            "name": "authenticate-policy-mismatch-conflicting",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(_encode({**commit, "policyId": "other-policy"})),
        },
        {
            "name": "authenticate-receipt-backup-mismatch",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(_encode(_receipt(backupId="other"))),
            "raw_commit": _hex(
                _encode(_commit_for(_encode(_receipt(backupId="other")), _receipt(backupId="other"), backupId=BACKUP))
            ),
        },
        {
            "name": "authenticate-missing-osd-corrupt",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(_encode(_receipt(objectSetDigest=""))),
            "raw_commit": _hex(raw_c),
        },
        {
            "name": "authenticate-objectdigest-fallback",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(object_digest_raw_r),
            "raw_commit": _hex(_encode(object_digest_commit)),
        },
        {
            "name": "authenticate-osd-type-mismatch-corrupt",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(number_osd_raw),
            "raw_commit": _hex(_encode(number_osd_commit)),
        },
        {
            "name": "authenticate-expected-mismatch-conflicting",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(raw_c),
            "expected_object_set_digest": "c" * 64,
        },
        {
            "name": "authenticate-expected-match",
            "op": "authenticate",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(raw_c),
            "expected_object_set_digest": OSD,
        },
        {
            "name": "committed-happy",
            "op": "authenticate-committed",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(raw_c),
        },
        {
            "name": "committed-empty-hash-skips",
            "op": "authenticate-committed",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(_encode({**commit, "commitHash": ""})),
        },
        {
            "name": "committed-hash-mismatch",
            "op": "authenticate-committed",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(_encode({**commit, "commitHash": "f" * 64})),
        },
        {
            "name": "committed-previous-mismatch",
            "op": "authenticate-committed",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(raw_c),
            "expected_previous_commit_hash": "d" * 64,
        },
        {
            "name": "committed-generation-mismatch",
            "op": "authenticate-committed",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(raw_c),
            "expected_target_generation": 9,
        },
        {
            "name": "committed-generation-string-match",
            "op": "authenticate-committed",
            "policy_id": POLICY,
            "backup_id": BACKUP,
            "raw_receipt": _hex(raw_r),
            "raw_commit": _hex(_encode({**commit, "targetGeneration": "1", "commitHash": bp._commit_hash({**commit, "targetGeneration": "1"})})),
            "expected_target_generation": 1,
        },
        {
            "name": "parent-happy",
            "op": "authenticate-parent",
            "policy_id": POLICY,
            "expected_parent_backup_id": PARENT,
            "raw_receipt": _hex(parent_raw_r),
            "raw_commit": _hex(parent_raw_c),
            "expected_receipt_digest": parent_commit["receiptDigest"],
            "expected_commit_hash": parent_commit["commitHash"],
            "expected_lineage_id": "lineage-frozen-1",
            "expected_object_set_digest": OSD,
        },
        {
            "name": "parent-missing",
            "op": "authenticate-parent",
            "policy_id": POLICY,
            "expected_parent_backup_id": PARENT,
            "raw_receipt": None,
            "raw_commit": None,
        },
        {
            "name": "parent-receipt-digest-mismatch",
            "op": "authenticate-parent",
            "policy_id": POLICY,
            "expected_parent_backup_id": PARENT,
            "raw_receipt": _hex(parent_raw_r),
            "raw_commit": _hex(parent_raw_c),
            "expected_receipt_digest": "0" * 64,
        },
        {
            "name": "parent-lineage-falsy-skips",
            "op": "authenticate-parent",
            "policy_id": POLICY,
            "expected_parent_backup_id": PARENT,
            "raw_receipt": _hex(_encode(_receipt(backupId=PARENT, lineageId=""))),
            "raw_commit": _hex(
                _encode(
                    _commit_for(
                        _encode(_receipt(backupId=PARENT, lineageId="")),
                        _receipt(backupId=PARENT, lineageId=""),
                        backupId=PARENT,
                        lineageId=None,
                    )
                )
            ),
            "expected_lineage_id": "lineage-other",
        },
        {
            "name": "parent-lineage-mismatch",
            "op": "authenticate-parent",
            "policy_id": POLICY,
            "expected_parent_backup_id": PARENT,
            "raw_receipt": _hex(parent_raw_r),
            "raw_commit": _hex(parent_raw_c),
            "expected_lineage_id": "lineage-other",
        },
        {
            "name": "commit-hash-stable-json",
            "op": "commit-hash",
            "commit": {k: v for k, v in commit.items() if k != "commitHash"},
        },
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
        "ast_matches_source_commit": {name: True for name in (*AST_NAMES, *PUBLISH_AST_NAMES)},
        "cases": cases,
    }
    path = ROOT / CORPUS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def test_v26_recovery_authenticate_matches_python_4_8_0() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v26/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["ast_matches_source_commit"] == {name: True for name in (*AST_NAMES, *PUBLISH_AST_NAMES)}
    for case in fixture["cases"]:
        assert replay(case) == case["expected"], case["name"]


if __name__ == "__main__":
    written = write_corpus()
    print(written)
