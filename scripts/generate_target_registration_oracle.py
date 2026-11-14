"""Offline oracle for target metadata; it never reads or writes a target directory.

The retained HTTP conversion function and _register run unchanged. Authentication
and the filesystem effect entry are isolated; physical reads are a separate gate.
Canonical qualification requires CPython 3.12. A developer may regenerate on a
different interpreter, but that result is explicitly not qualified.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import unicodedata
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.workspace import backup_targets  # noqa: E402
from deepseek_infra.web.http_utils import read_json_body  # noqa: E402

CORPUS = ROOT / "go/internal/targets/testdata/filesystem_registration_v1.json"
UNICODE_15_CLASSIFICATION_SHA256 = "cd2d91bba98246cca589515d2996fbda8dc1816816065797f51c4c92fddf7087"


def cases() -> list[dict]:
    markers = [
        {"targetId": "target_oracle", "targetNonce": "original", "createdAt": "2000-01-01T00:00:00Z"},
        {"targetId": "target_oracle", "targetNonce": [True, {"b": False, "a": 1}]},
        {"targetId": "target_oracle", "targetNonce": 1.0, "createdAt": False},
    ]
    inputs = [
        {}, {"label": {"b": True, "a": [1, None, "x"]}},
        {"label": 1.0, "priority": "١_٢"}, {"priority": 1e20}, {"priority": True},
        {"region": "  ", "failureDomain": ["a", False]},
        {"storageCostPerGiBMonth": True, "egressCostPerGiB": "١_٢.٥"},
        {"maxReadBytesPerSecond": 1.0}, {"priority": "bad"}, {"storageCostPerGiBMonth": "bad"},
        {"storageCostPerGiBMonth": "nan"}, {"egressCostPerGiB": -1}, {"maxConcurrentTransfers": False},
        {"label": "a\u2028b\x1c", "provider": "\x1c provider \x1f"}, {"priority": {"a": 1}},
        {"region": {"b": False, "a": 2}},
    ]
    rows = [dict(input=value, marker=marker) for value in inputs for marker in markers]
    for value in [
        {"priority": "\x1c1\x1c"}, {"priority": "\x851\x85"}, {"priority": "\u20001\u2000"},
        {"priority": -1.9}, {"priority": []}, {"priority": [1]},
        {"egressCostPerGiB": "\x1c1\x1c"}, {"egressCostPerGiB": "0x1p1"},
        {"egressCostPerGiB": "+Infinity"}, {"egressCostPerGiB": "1e999"},
        {"egressCostPerGiB": "1e-999"}, {"egressCostPerGiB": "1__2"},
        {"egressCostPerGiB": []}, {"egressCostPerGiB": 10**400},
        {"maxReadBytesPerSecond": 9007199254740993}, {"maxWriteBytesPerSecond": "10"},
        {"maxConcurrentTransfers": 0}, {"maxConcurrentTransfers": -1},
        {"label": ["a'b", 'a"b', "both'\"", "\u00a0", "\u0378", "\U0001e4d0"]},
        {"priority": "𝟙𝟚", "storageCostPerGiBMonth": ".5_0e+2"},
        {"provider": [False, None], "jurisdiction": {}, "costClass": 0},
    ]:
        rows.append(dict(input=value, marker=markers[0]))
    rows.append(dict(inputRaw='{"label":{"b":1,"a":2,"b":3},"priority":9007199254740993}', marker=markers[0]))
    rows.append(dict(inputRaw='{"priority":1e999}', marker=markers[0]))
    rows.append(dict(inputRaw='{"label":[-0,1e999,-1e999]}', marker=markers[0]))
    return rows


async def evaluate(rows: list[dict]) -> list[dict]:
    route_path = ROOT / "deepseek_infra/web/routes/backup_governance.py"
    source = route_path.read_text(encoding="utf8")
    route = next(node for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_backup_targets_create")
    route.decorator_list = []
    marker: dict = {}

    def register(path, **kwargs):
        # The original init_target performs these marker coercions before _register.
        if not isinstance(marker, dict) or not marker.get("targetId"):
            raise AppError("Backup target marker is invalid", code="invalid_payload")
        return backup_targets._register(path, str(marker["targetId"]), str(marker.get("targetNonce") or ""),
                                        created_at=marker.get("createdAt"), **kwargs)

    saved = backup_targets.init_target, backup_targets._store_target, backup_targets._utc_iso
    backup_targets.init_target = register

    def isolated_store(record: dict[str, Any], *, expected_generation: int | None = None) -> dict[str, Any]:
        return record

    backup_targets._store_target = isolated_store
    backup_targets._utc_iso = lambda: "2000-01-01T00:00:00Z"
    namespace: dict[str, Any] = dict(Request=object, JSONResponse=object, Path=PurePosixPath, require_api_auth=lambda _: None,
                     read_json_body=read_json_body, json_response=lambda result: result,
                     backup_targets=backup_targets, AppError=AppError)
    exec(compile(ast.Module(body=[route], type_ignores=[]), str(route_path), "exec"), namespace)

    class RequestFixture:
        def __init__(self, raw: bytes):
            self.raw = raw
            self.headers = {"Content-Length": str(len(raw))}

        async def body(self):
            return self.raw

    result = []
    try:
        for index, value in enumerate(rows):
            marker = value["marker"]
            raw = value.get("inputRaw") or json.dumps(value["input"], ensure_ascii=True, separators=(",", ":"))
            row = dict(value, name=f"retained-metadata-{index:03d}", path="/retained/target", now=946684800)
            # Filesystem work is deliberately replaced; this tests normalized metadata only.
            payload = json.loads(raw)
            payload.setdefault("path", row["path"])
            if "inputRaw" in row:
                row["inputRaw"] = raw[:-1] + ',"path":"/retained/target"}'
            else:
                row["input"] = payload
            try:
                document = await namespace["api_backup_targets_create"](RequestFixture(
                    row.get("inputRaw", json.dumps(payload, ensure_ascii=True, separators=(",", ":"))).encode()))
                row.update(status="ok", document=document)
            except Exception as error:
                row.update(status="error", errorKind=type(error).__name__, httpStatus=getattr(error, "status", 500),
                           message=str(error))
                if isinstance(error, AppError):
                    row["errorCode"] = str(getattr(error.code, "value", error.code))
            result.append(row)
    finally:
        backup_targets.init_target, backup_targets._store_target, backup_targets._utc_iso = saved
    return result


def unicode_digest() -> str:
    digest = hashlib.sha256()
    for number in range(0x110000):
        value = chr(number)
        digest.update(bytes((value.isprintable(), value.isspace(), unicodedata.decimal(value, 255))))
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--allow-developer-version", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    canonical = sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 12) and unicodedata.unidata_version == "15.0.0"
    if not canonical and not args.allow_developer_version:
        raise SystemExit("Canonical oracle requires CPython 3.12 / Unicode 15.0.0")
    result: dict[str, Any] = dict(schemaVersion=1, oracle="retained HTTP function and unmodified backup_targets._register; metadata only",
                  cases=asyncio.run(evaluate(cases())))
    serialized = (json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    if args.write:
        CORPUS.write_bytes(serialized)
    else:
        assert CORPUS.read_bytes() == serialized, "Frozen metadata oracle drifted"
    # Non-finite JSON inputs are stored in inputRaw so the checked corpus remains JSON.
    proof = dict(status="PASS" if canonical else "DEVELOPER_ONLY_UNQUALIFIED", python=sys.version.split()[0],
                 unicodeVersion=unicodedata.unidata_version, cases=len(result["cases"]),
                 corpusSha256=hashlib.sha256(serialized).hexdigest(), unicodeClassificationSha256=unicode_digest(),
                 sources={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
                     "deepseek_infra/web/routes/backup_governance.py", "deepseek_infra/infra/workspace/backup_targets.py",
                     "deepseek_infra/web/http_utils.py")}, physicalFilesystemQualified=False, productionPythonRequired=False,
                 publicRegistrationQualified=False, fullMigrationQualified=False, readiness="NOT_READY")
    if canonical:
        assert proof["unicodeClassificationSha256"] == UNICODE_15_CLASSIFICATION_SHA256, "Python classification differs from official Unicode 15 data"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf8")
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
