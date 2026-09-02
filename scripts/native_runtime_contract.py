#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP_PATH = ROOT / "release" / "native_runtime_ownership_v1.json"
TOOLCHAIN_PATH = ROOT / "release" / "native_runtime_toolchain_v1.json"
DESCRIPTOR_PATH = ROOT / "proto" / "generated" / "descriptor.v1.json"
CORPUS_MANIFEST = ROOT / "compat" / "native-runtime" / "v1" / "manifest.json"
PROTO_ROOT = ROOT / "proto"

SECRET_FIELD_FRAGMENTS = (
    "private_key",
    "secret",
    "password",
    "credential",
    "passphrase",
    "access_key",
    "secret_key",
    "age_identity",
)
PRODUCTION_OWNERS = {"rust", "go"}
TARGET_OWNERS = {"rust", "go", "python", "typescript"}
STORES = {"python_oracle", "go_control", "rust_data"}


class ContractError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractError(f"{path} must be a JSON object")
    return data


def load_ownership(path: Path = OWNERSHIP_PATH) -> dict[str, Any]:
    return _load_json(path)


def validate_ownership(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise ContractError("ownership schema_version must be 1")
    if data.get("status") != "accepted":
        raise ContractError("ownership contract must be accepted")
    if data.get("current_production_authority") != "python":
        raise ContractError("4.8.1 production authority must remain python")
    if data.get("source_commit") != "a37735c68398fc8f795babaa269e2de6a5acd567":
        raise ContractError("ownership source_commit must freeze 4.8.0 merge SHA")
    domains = data.get("domains")
    if not isinstance(domains, list) or not domains:
        raise ContractError("domains must be a non-empty list")
    seen: set[str] = set()
    store_writer = {"go_control": "go", "rust_data": "rust", "python_oracle": "python"}
    for item in domains:
        if not isinstance(item, dict):
            raise ContractError("each domain must be an object")
        domain_id = str(item.get("id") or "")
        if not domain_id or domain_id in seen:
            raise ContractError(f"duplicate or empty domain id: {domain_id!r}")
        seen.add(domain_id)
        current = str(item.get("current_owner") or "")
        target = str(item.get("target_owner") or "")
        if current not in {"python", "typescript"}:
            raise ContractError(f"{domain_id} current_owner must be python or typescript")
        if target not in TARGET_OWNERS:
            raise ContractError(f"{domain_id} target_owner is invalid")
        production = item.get("production", True)
        if production is True and target not in PRODUCTION_OWNERS and target != "python":
            raise ContractError(f"{domain_id} production target_owner is invalid")
        if production is True and target == "python" and item.get("plane") != "reference":
            raise ContractError(f"{domain_id} cannot remain python-owned in production 5.0")
        store = item.get("durable_store")
        if store not in (None, *STORES):
            raise ContractError(f"{domain_id} durable_store is invalid")
        if isinstance(store, str) and production is True:
            expected_writer = store_writer[store]
            if target != expected_writer:
                raise ContractError(f"{domain_id} store {store} target_owner must be {expected_writer}")
    for name, spec in data.get("durable_stores", {}).items():
        if not isinstance(spec, dict):
            raise ContractError(f"store {name} must be an object")
        writer = spec.get("writer")
        shared = spec.get("shared_with") or []
        if shared:
            raise ContractError(f"store {name} cannot be shared")
        if name == "go_control" and writer != "go":
            raise ContractError("go_control writer must be go")
        if name == "rust_data" and writer != "rust":
            raise ContractError("rust_data writer must be rust")
        if name == "python_oracle" and writer != "python":
            raise ContractError("python_oracle writer must be python")
    forbidden = set(data.get("forbidden") or [])
    for item in (
        "shared_cross_language_sqlite_writes",
        "cgo_ffi_primary_architecture",
        "python_production_owner_at_5_0",
        "permanent_python_fallback",
    ):
        if item not in forbidden:
            raise ContractError(f"missing forbidden rule {item}")
    production_python = [
        item["id"]
        for item in domains
        if item.get("production", True) is True and item.get("target_owner") == "python"
    ]
    if production_python:
        raise ContractError(f"5.0 production python owners are forbidden: {production_python}")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*?$", "", text, flags=re.M)


def _extract_block(text: str, start: int) -> tuple[str, int]:
    brace = text.find("{", start)
    if brace < 0:
        raise ContractError("expected proto block")
    depth = 0
    for index in range(brace, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1 : index], index + 1
    raise ContractError("unbalanced proto block")


_FIELD_RE = re.compile(
    r"(?:(repeated|optional)\s+)?((?:[A-Za-z_][\w.]*))\s+([A-Za-z_]\w*)\s*=\s*(\d+)\s*;"
)
_ENUM_VALUE_RE = re.compile(r"([A-Z][A-Z0-9_]*)\s*=\s*(\d+)\s*;")
_RPC_RE = re.compile(r"rpc\s+(\w+)\s*\(\s*([\w.]+)\s*\)\s*returns\s*\(\s*([\w.]+)\s*\)\s*;")


def parse_proto(path: Path) -> dict[str, Any]:
    raw = _strip_comments(path.read_text(encoding="utf-8"))
    package_match = re.search(r"package\s+([\w.]+)\s*;", raw)
    syntax_match = re.search(r'syntax\s*=\s*"([^"]+)"\s*;', raw)
    if syntax_match is None or syntax_match.group(1) != "proto3":
        raise ContractError(f"{path} must declare syntax proto3")
    if package_match is None:
        raise ContractError(f"{path} must declare a package")
    try:
        proto_path = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        proto_path = path.name
    document: dict[str, Any] = {
        "path": proto_path,
        "package": package_match.group(1),
        "messages": [],
        "enums": [],
        "services": [],
        "imports": re.findall(r'import\s+"([^"]+)"\s*;', raw),
    }
    index = 0
    while index < len(raw):
        message_match = re.search(r"\b(message|enum|service)\s+(\w+)\s*\{", raw[index:])
        if message_match is None:
            break
        kind, name = message_match.group(1), message_match.group(2)
        abs_start = index + message_match.start()
        body, next_index = _extract_block(raw, abs_start)
        if kind == "message":
            fields = []
            numbers: set[int] = set()
            for field in _FIELD_RE.finditer(body):
                number = int(field.group(4))
                if number in numbers:
                    raise ContractError(f"{path} {name} reuses field number {number}")
                numbers.add(number)
                field_name = field.group(3)
                if any(fragment in field_name for fragment in SECRET_FIELD_FRAGMENTS):
                    raise ContractError(f"{path} forbids secret-bearing field {field_name}")
                fields.append(
                    {
                        "label": field.group(1) or "singular",
                        "type": field.group(2),
                        "name": field_name,
                        "number": number,
                    }
                )
            document["messages"].append({"name": name, "fields": fields})
        elif kind == "enum":
            values = []
            for value in _ENUM_VALUE_RE.finditer(body):
                values.append({"name": value.group(1), "number": int(value.group(2))})
            if not values or int(values[0]["number"]) != 0:
                raise ContractError(f"{path} enum {name} must start at 0")
            if not str(values[0]["name"]).endswith("_UNSPECIFIED"):
                raise ContractError(f"{path} enum {name} zero value must be UNSPECIFIED")
            document["enums"].append({"name": name, "values": values})
        else:
            rpcs = [
                {"name": item.group(1), "request": item.group(2), "response": item.group(3)}
                for item in _RPC_RE.finditer(body)
            ]
            document["services"].append({"name": name, "rpcs": rpcs})
        index = next_index
    return document


def collect_protos(root: Path = PROTO_ROOT) -> list[dict[str, Any]]:
    files = sorted(path for path in root.rglob("*.proto") if "generated" not in path.parts)
    if not files:
        raise ContractError("no proto sources found")
    return [parse_proto(path) for path in files]


def build_descriptor(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "syntax": "proto3",
        "source_commit": "a37735c68398fc8f795babaa269e2de6a5acd567",
        "files": documents,
    }


def descriptor_bytes(descriptor: dict[str, Any]) -> bytes:
    return (json.dumps(descriptor, indent=2, sort_keys=True) + "\n").encode("utf-8")


def validate_descriptor_invariants(descriptor: dict[str, Any]) -> None:
    fence_found = False
    unknown_found = False
    control_mutation_rpcs = []
    for document in descriptor["files"]:
        for enum in document["enums"]:
            if enum["name"] == "EffectState":
                names = {item["name"] for item in enum["values"]}
                if "EFFECT_STATE_UNKNOWN" not in names:
                    raise ContractError("EffectState must include UNKNOWN")
                if "EFFECT_STATE_UNSPECIFIED" not in names:
                    raise ContractError("EffectState must include UNSPECIFIED")
                unknown_found = True
        for message in document["messages"]:
            field_names = {item["name"] for item in message["fields"]}
            if message["name"] == "ActionFence":
                if field_names != {"action_id", "execution_epoch"}:
                    raise ContractError("ActionFence must be action_id + execution_epoch")
                fence_found = True
            nested = [item for item in message["fields"] if item["type"].endswith("ActionFence")]
            if message["name"].endswith("Request") or message["name"].endswith("Result"):
                if message["name"] not in {"HealthRequest", "ShadowEvaluateRequest"} and not nested:
                    if "fence" not in field_names and not {"action_id", "execution_epoch"} <= field_names:
                        if document["package"] not in {"deepseek.common.v1", "deepseek.control.v1"}:
                            raise ContractError(f"{message['name']} must bind an ActionFence")
        if document["package"] == "deepseek.control.v1":
            for service in document["services"]:
                for rpc in service["rpcs"]:
                    if rpc["name"] not in {"Health", "ShadowEvaluate"}:
                        control_mutation_rpcs.append(rpc["name"])
    if not fence_found:
        raise ContractError("ActionFence is missing")
    if not unknown_found:
        raise ContractError("EffectState UNKNOWN is missing")
    if control_mutation_rpcs:
        raise ContractError(f"4.8.1 control proto cannot expose mutation RPCs: {control_mutation_rpcs}")


def write_descriptor(path: Path = DESCRIPTOR_PATH) -> dict[str, Any]:
    descriptor = build_descriptor(collect_protos())
    validate_descriptor_invariants(descriptor)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = descriptor_bytes(descriptor)
    path.write_bytes(rendered)
    return descriptor


def check_descriptor(path: Path = DESCRIPTOR_PATH) -> dict[str, Any]:
    expected = build_descriptor(collect_protos())
    validate_descriptor_invariants(expected)
    if not path.is_file():
        raise ContractError(f"missing generated descriptor: {path}")
    actual = path.read_bytes()
    if actual != descriptor_bytes(expected):
        raise ContractError("generated proto descriptor drifted from proto sources")
    return expected


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def validate_corpus(manifest_path: Path = CORPUS_MANIFEST) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != 1:
        raise ContractError("corpus schema_version must be 1")
    if manifest.get("source_commit") != "a37735c68398fc8f795babaa269e2de6a5acd567":
        raise ContractError("corpus source_commit must freeze 4.8.0 merge SHA")
    corpora = manifest.get("corpora")
    if not isinstance(corpora, list) or not corpora:
        raise ContractError("corpora must be a non-empty list")
    seen: set[str] = set()
    for item in corpora:
        corpus_id = str(item.get("id") or "")
        rel = str(item.get("path") or "")
        if not corpus_id or corpus_id in seen:
            raise ContractError(f"invalid corpus id {corpus_id!r}")
        seen.add(corpus_id)
        path = ROOT / rel
        if not path.is_file():
            raise ContractError(f"corpus file missing: {rel}")
        digest = sha256_file(path)
        if digest != item.get("sha256"):
            raise ContractError(f"corpus digest mismatch for {corpus_id}: {digest}")
        if item.get("sensitivity") not in {"public", "redacted"}:
            raise ContractError(f"{corpus_id} sensitivity must be public or redacted")
    return manifest


def validate_toolchain(path: Path = TOOLCHAIN_PATH) -> dict[str, Any]:
    data = _load_json(path)
    go_raw = data.get("go")
    protoc_raw = data.get("protoc")
    go: dict[str, Any] = go_raw if isinstance(go_raw, dict) else {}
    protoc: dict[str, Any] = protoc_raw if isinstance(protoc_raw, dict) else {}
    if go.get("version") != "1.27.1":
        raise ContractError("toolchain must pin Go 1.27.1")
    if protoc.get("version_line") != "36.x":
        raise ContractError("toolchain must pin protoc 36.x")
    if protoc.get("syntax") != "proto3":
        raise ContractError("toolchain syntax must remain proto3")
    return data


def check_all() -> dict[str, Any]:
    ownership = load_ownership()
    validate_ownership(ownership)
    toolchain = validate_toolchain()
    descriptor = check_descriptor()
    corpus = validate_corpus()
    return {
        "ok": True,
        "domains": len(ownership["domains"]),
        "proto_files": len(descriptor["files"]),
        "corpora": len(corpus["corpora"]),
        "go": toolchain["go"]["version"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native runtime contract freeze checks")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-descriptor", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.write_descriptor:
            write_descriptor()
        report = check_all()
    except ContractError as exc:
        print(f"native runtime contract FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
