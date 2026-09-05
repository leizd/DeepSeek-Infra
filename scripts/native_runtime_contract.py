#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP_PATH = ROOT / "release" / "native_runtime_ownership_v1.json"
TOOLCHAIN_PATH = ROOT / "release" / "native_runtime_toolchain_v1.json"
DESCRIPTOR_PATH = ROOT / "proto" / "generated" / "descriptor.v1.json"
BINARY_DESCRIPTOR_PATH = ROOT / "proto" / "generated" / "descriptor.pb"
CODEGEN_MANIFEST_PATH = ROOT / "proto" / "generated" / "codegen-manifest.v1.json"
CORPUS_MANIFEST = ROOT / "compat" / "native-runtime" / "v1" / "manifest.json"
CORPUS_MANIFESTS = (
    ROOT / "compat" / "native-runtime" / "v1" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v2" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v3" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v4" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v5" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v6" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v8" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v9" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v10" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v11" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v12" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v13" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v14" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v15" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v16" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v17" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v18" / "manifest.json",
    ROOT / "compat" / "native-runtime" / "v19" / "manifest.json",
)
COMMAND_CODES_PATH = ROOT / "release" / "native_runtime_command_codes_v1.json"
PROTO_ROOT = ROOT / "proto"
FROZEN_DESCRIPTOR_SHA256 = "088cc0348e01d40bcde8dcda8dbaeaeae53a5282aebaa66b84be4627ca5086f4"
PINNED_PYTHON_PROTOBUF_VERSION = "7.35.1"

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
    production_python = [item["id"] for item in domains if item.get("production", True) is True and item.get("target_owner") == "python"]
    if production_python:
        raise ContractError(f"5.0 production python owners are forbidden: {production_python}")


_SCALAR_FIELD_TYPES = {
    1: "double",
    2: "float",
    3: "int64",
    4: "uint64",
    5: "int32",
    6: "fixed64",
    7: "fixed32",
    8: "bool",
    9: "string",
    12: "bytes",
    13: "uint32",
    15: "sfixed32",
    16: "sfixed64",
    17: "sint32",
    18: "sint64",
}


def _protobuf_modules() -> tuple[Any, type[Exception]]:
    try:
        import google.protobuf
        from google.protobuf import descriptor_pb2
        from google.protobuf.message import DecodeError
    except ImportError as exc:
        raise ContractError(f"pinned protobuf validator is unavailable: {exc}") from exc
    if google.protobuf.__version__ != PINNED_PYTHON_PROTOBUF_VERSION:
        raise ContractError(
            "protobuf validator version mismatch: "
            f"expected {PINNED_PYTHON_PROTOBUF_VERSION}, got {google.protobuf.__version__}"
        )
    return descriptor_pb2, DecodeError


def load_binary_descriptor(path: Path = BINARY_DESCRIPTOR_PATH) -> Any:
    descriptor_pb2, decode_error = _protobuf_modules()
    if not path.is_file():
        raise ContractError(f"missing generated binary protobuf descriptor: {path}")
    descriptor_set = descriptor_pb2.FileDescriptorSet()
    payload = path.read_bytes()
    try:
        consumed = descriptor_set.ParseFromString(payload)
    except decode_error as exc:
        raise ContractError(f"invalid binary protobuf descriptor: {path}") from exc
    if consumed != len(payload) or not descriptor_set.file:
        raise ContractError(f"incomplete or empty binary protobuf descriptor: {path}")
    return descriptor_set


def _display_type(package: str, field_type: int, type_name: str) -> str:
    scalar = _SCALAR_FIELD_TYPES.get(field_type)
    if scalar is not None:
        return scalar
    if field_type not in {11, 14} or not type_name.startswith("."):
        raise ContractError(f"unsupported or unresolved protobuf field type {field_type}: {type_name!r}")
    symbol = type_name[1:]
    local_prefix = f"{package}."
    return symbol[len(local_prefix) :] if symbol.startswith(local_prefix) else symbol


def _options_hex(options: Any) -> str:
    return options.SerializeToString(deterministic=True).hex()


def descriptor_set_to_contract(descriptor_set: Any) -> dict[str, Any]:
    """Project protoc's FileDescriptorSet into the frozen contract model.

    All security and compatibility gates consume this projection. Source text is
    intentionally never parsed with regular expressions.
    """

    documents: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for file_descriptor in descriptor_set.file:
        file_name = str(file_descriptor.name)
        path = PurePosixPath(file_name)
        if not file_name or path.is_absolute() or ".." in path.parts or file_name in seen_files:
            raise ContractError(f"invalid or duplicate protobuf descriptor path: {file_name!r}")
        seen_files.add(file_name)
        package = str(file_descriptor.package)
        if not package:
            raise ContractError(f"{file_name} must declare a package")
        if file_descriptor.syntax != "proto3":
            raise ContractError(f"{file_name} must declare syntax proto3")

        messages: list[dict[str, Any]] = []
        enums: list[dict[str, Any]] = []

        def append_enum(enum: Any, prefix: str = "") -> None:
            name = f"{prefix}{enum.name}"
            values = [
                {
                    "name": str(value.name),
                    "number": int(value.number),
                    "options_hex": _options_hex(value.options),
                }
                for value in enum.value
            ]
            enums.append({"name": name, "values": values, "options_hex": _options_hex(enum.options)})

        def append_message(message: Any, prefix: str = "") -> None:
            name = f"{prefix}{message.name}"
            oneof_names = [str(item.name) for item in message.oneof_decl]
            if not str(message.name) or len(oneof_names) != len(set(oneof_names)) or any(not item for item in oneof_names):
                raise ContractError(f"{file_name} has an invalid message or oneof name in {name!r}")
            numbers: set[int] = set()
            names: set[str] = set()
            fields: list[dict[str, Any]] = []
            for field in message.field:
                number = int(field.number)
                field_name = str(field.name)
                if number <= 0 or number in numbers or not field_name or field_name in names:
                    raise ContractError(f"{file_name} {name} has an invalid or duplicate field {field_name!r}={number}")
                numbers.add(number)
                names.add(field_name)
                if field.label == 3:
                    label = "repeated"
                elif field.label == 1:
                    label = "optional" if field.proto3_optional else "singular"
                else:
                    raise ContractError(f"{file_name} {name}.{field_name} uses a non-proto3 field label")
                oneof_index = int(field.oneof_index) if field.HasField("oneof_index") else None
                if oneof_index is not None and not 0 <= oneof_index < len(oneof_names):
                    raise ContractError(f"{file_name} {name}.{field_name} has an invalid oneof index")
                if field.proto3_optional and oneof_index is None:
                    raise ContractError(f"{file_name} {name}.{field_name} has invalid proto3 optional presence")
                fields.append(
                    {
                        "label": label,
                        "type": _display_type(package, int(field.type), str(field.type_name)),
                        "name": field_name,
                        "number": number,
                        "oneof_index": oneof_index,
                        "oneof_name": oneof_names[oneof_index] if oneof_index is not None else None,
                        "proto3_optional": bool(field.proto3_optional),
                        "json_name": str(field.json_name),
                        "default_value": str(field.default_value),
                        "extendee": str(field.extendee),
                        "options_hex": _options_hex(field.options),
                    }
                )
            messages.append(
                {
                    "name": name,
                    "fields": fields,
                    "oneofs": oneof_names,
                    "options_hex": _options_hex(message.options),
                }
            )
            nested_prefix = f"{name}."
            for nested_enum in message.enum_type:
                append_enum(nested_enum, nested_prefix)
            for nested_message in message.nested_type:
                append_message(nested_message, nested_prefix)

        for enum in file_descriptor.enum_type:
            append_enum(enum)
        for message in file_descriptor.message_type:
            append_message(message)

        services = []
        for service in file_descriptor.service:
            rpcs = [
                {
                    "name": str(method.name),
                    "request": _display_type(package, 11, str(method.input_type)),
                    "response": _display_type(package, 11, str(method.output_type)),
                    "client_streaming": bool(method.client_streaming),
                    "server_streaming": bool(method.server_streaming),
                    "options_hex": _options_hex(method.options),
                }
                for method in service.method
            ]
            services.append(
                {
                    "name": str(service.name),
                    "rpcs": rpcs,
                    "options_hex": _options_hex(service.options),
                }
            )

        documents.append(
            {
                "path": f"proto/{file_name}",
                "package": package,
                "messages": messages,
                "enums": enums,
                "services": services,
                "imports": [str(item) for item in file_descriptor.dependency],
                "options_hex": _options_hex(file_descriptor.options),
            }
        )
    return {
        "schema_version": 1,
        "syntax": "proto3",
        "source_commit": "a37735c68398fc8f795babaa269e2de6a5acd567",
        "files": documents,
    }


def validate_descriptor_invariants(descriptor: dict[str, Any]) -> None:
    fence_found = False
    unknown_found = False
    control_mutation_rpcs: list[str] = []
    for document in descriptor["files"]:
        for enum in document["enums"]:
            values = enum.get("values") or []
            if not values or int(values[0].get("number", -1)) != 0:
                raise ContractError(f"{document['path']} enum {enum.get('name')} must start at 0")
            if not str(values[0].get("name") or "").endswith("_UNSPECIFIED"):
                raise ContractError(f"{document['path']} enum {enum.get('name')} zero value must be UNSPECIFIED")
            if enum["name"] == "EffectState":
                names = {item["name"] for item in values}
                if "EFFECT_STATE_UNKNOWN" not in names:
                    raise ContractError("EffectState must include UNKNOWN")
                if "EFFECT_STATE_UNSPECIFIED" not in names:
                    raise ContractError("EffectState must include UNSPECIFIED")
                unknown_found = True
        for message in document["messages"]:
            for field in message["fields"]:
                field_name = str(field.get("name") or "")
                if any(fragment in field_name for fragment in SECRET_FIELD_FRAGMENTS):
                    raise ContractError(f"{document['path']} forbids secret-bearing field {field_name}")
            field_names = {item["name"] for item in message["fields"]}
            if message["name"] == "ActionFence":
                fence_shape = {
                    (item["name"], int(item["number"]), item["type"], item.get("label", "singular"))
                    for item in message["fields"]
                }
                if fence_shape != {
                    ("action_id", 1, "string", "singular"),
                    ("execution_epoch", 2, "uint64", "singular"),
                }:
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


def _symbols(descriptor: dict[str, Any], category: str) -> dict[tuple[str, str], dict[str, Any]]:
    symbols: dict[tuple[str, str], dict[str, Any]] = {}
    for document in descriptor.get("files", []):
        package = str(document.get("package") or "")
        for item in document.get(category, []):
            key = (package, str(item.get("name") or ""))
            if not all(key) or key in symbols:
                raise ContractError(f"invalid or duplicate protobuf {category} symbol: {key}")
            symbols[key] = item
    return symbols


def _rpc_shape(rpc: dict[str, Any]) -> tuple[str, str, bool, bool]:
    return (
        str(rpc.get("request") or ""),
        str(rpc.get("response") or ""),
        bool(rpc.get("client_streaming", False)),
        bool(rpc.get("server_streaming", False)),
    )


def _snake_to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(item[:1].upper() + item[1:] for item in tail)


def _field_shape(field: dict[str, Any]) -> tuple[str, str, str, int | None, str | None, bool, str, str, str, str]:
    name = str(field.get("name") or "")
    label = str(field.get("label") or "singular")
    oneof_index_raw = field.get("oneof_index")
    oneof_index = int(oneof_index_raw) if oneof_index_raw is not None else None
    return (
        name,
        str(field.get("type") or ""),
        label,
        oneof_index,
        str(field["oneof_name"]) if field.get("oneof_name") is not None else None,
        bool(field.get("proto3_optional", label == "optional")),
        str(field.get("json_name") or _snake_to_camel(name)),
        str(field.get("default_value") or ""),
        str(field.get("extendee") or ""),
        str(field.get("options_hex") or ""),
    )


def validate_descriptor_compatibility(baseline: dict[str, Any], current: dict[str, Any]) -> None:
    """Enforce additive-only evolution against the immutable v1 baseline."""

    baseline_messages = _symbols(baseline, "messages")
    current_messages = _symbols(current, "messages")
    for key, baseline_message in baseline_messages.items():
        current_message = current_messages.get(key)
        if current_message is None:
            raise ContractError(f"breaking protobuf change: removed message {'.'.join(key)}")
        if str(current_message.get("options_hex") or "") != str(baseline_message.get("options_hex") or ""):
            raise ContractError(f"breaking protobuf change: message options changed for {'.'.join(key)}")
        current_fields = {int(field["number"]): field for field in current_message.get("fields", [])}
        for baseline_field in baseline_message.get("fields", []):
            number = int(baseline_field["number"])
            current_field = current_fields.get(number)
            expected_field = _field_shape(baseline_field)
            actual = _field_shape(current_field or {})
            if current_field is None or actual != expected_field:
                raise ContractError(f"breaking protobuf change: {'.'.join(key)} field {number} changed from {expected_field} to {actual}")

    baseline_enums = _symbols(baseline, "enums")
    current_enums = _symbols(current, "enums")
    for key, baseline_enum in baseline_enums.items():
        current_enum = current_enums.get(key)
        if current_enum is None:
            raise ContractError(f"breaking protobuf change: removed enum {'.'.join(key)}")
        if str(current_enum.get("options_hex") or "") != str(baseline_enum.get("options_hex") or ""):
            raise ContractError(f"breaking protobuf change: enum options changed for {'.'.join(key)}")
        current_values = {
            int(value["number"]): (str(value.get("name") or ""), str(value.get("options_hex") or ""))
            for value in current_enum.get("values", [])
        }
        for baseline_value in baseline_enum.get("values", []):
            number = int(baseline_value["number"])
            expected_value = (str(baseline_value.get("name") or ""), str(baseline_value.get("options_hex") or ""))
            if current_values.get(number) != expected_value:
                raise ContractError(f"breaking protobuf change: {'.'.join(key)} value {number} changed from {expected_value!r}")

    baseline_services = _symbols(baseline, "services")
    current_services = _symbols(current, "services")
    for key, baseline_service in baseline_services.items():
        current_service = current_services.get(key)
        if current_service is None:
            raise ContractError(f"breaking protobuf change: removed service {'.'.join(key)}")
        if str(current_service.get("options_hex") or "") != str(baseline_service.get("options_hex") or ""):
            raise ContractError(f"breaking protobuf change: service options changed for {'.'.join(key)}")
        current_rpcs = {str(rpc.get("name") or ""): rpc for rpc in current_service.get("rpcs", [])}
        for baseline_rpc in baseline_service.get("rpcs", []):
            name = str(baseline_rpc.get("name") or "")
            current_rpc = current_rpcs.get(name)
            options_changed = str((current_rpc or {}).get("options_hex") or "") != str(baseline_rpc.get("options_hex") or "")
            if current_rpc is None or _rpc_shape(current_rpc) != _rpc_shape(baseline_rpc) or options_changed:
                raise ContractError(f"breaking protobuf change: {'.'.join(key)}.{name} changed or was removed")


def check_descriptor(path: Path = DESCRIPTOR_PATH, binary_path: Path = BINARY_DESCRIPTOR_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"missing frozen descriptor baseline: {path}")
    if sha256_file(path) != FROZEN_DESCRIPTOR_SHA256:
        raise ContractError("frozen v1 descriptor baseline digest changed")
    baseline = _load_json(path)
    validate_descriptor_invariants(baseline)
    current = descriptor_set_to_contract(load_binary_descriptor(binary_path))
    validate_descriptor_invariants(current)
    validate_descriptor_compatibility(baseline, current)
    return current


def sha256_file(path: Path) -> str:
    payload = path.read_bytes()
    if path.suffix in {".go", ".json", ".proto"}:
        payload = payload.replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


def _checked_repo_path(relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ContractError(f"generated path must be repository-relative: {relative}")
    resolved_root = ROOT.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ContractError(f"generated path escapes repository: {relative}") from exc
    return resolved


def validate_codegen_manifest(path: Path = CODEGEN_MANIFEST_PATH) -> dict[str, Any]:
    data = _load_json(path)
    if data.get("schema_version") != 1:
        raise ContractError("codegen manifest schema_version must be 1")
    toolchain = validate_toolchain()
    expected_versions = {
        "protoc": toolchain["protoc"]["version"],
        **{
            name: toolchain["generators"][name]["version"]
            for name in ("protoc_gen_go", "protoc_gen_go_grpc", "prost", "tonic_build", "protox")
        },
    }
    if data.get("toolchain") != expected_versions:
        raise ContractError("codegen manifest toolchain drifted from the toolchain lock")

    sources = data.get("sources")
    if not isinstance(sources, list):
        raise ContractError("codegen manifest sources must be a list")
    expected_sources = {proto.relative_to(ROOT).as_posix() for proto in PROTO_ROOT.rglob("*.proto") if "generated" not in proto.parts}
    manifest_sources: set[str] = set()
    for entry in sources:
        if not isinstance(entry, dict):
            raise ContractError("codegen source entry must be an object")
        relative = str(entry.get("path") or "")
        if not relative or relative in manifest_sources:
            raise ContractError(f"duplicate or empty codegen source path: {relative!r}")
        manifest_sources.add(relative)
        source = _checked_repo_path(relative)
        if not source.is_file() or sha256_file(source) != entry.get("sha256"):
            raise ContractError(f"codegen source digest mismatch: {relative}")
    if manifest_sources != expected_sources:
        raise ContractError("codegen source file set drifted")

    outputs = data.get("outputs")
    if not isinstance(outputs, list):
        raise ContractError("codegen manifest outputs must be a list")
    manifest_outputs: set[str] = set()
    for entry in outputs:
        if not isinstance(entry, dict):
            raise ContractError("codegen output entry must be an object")
        relative = str(entry.get("path") or "")
        allowed = relative == "proto/generated/descriptor.pb" or (
            relative.startswith("go/internal/protocol/") and relative.endswith(".pb.go")
        )
        if not allowed or relative in manifest_outputs:
            raise ContractError(f"invalid or duplicate codegen output path: {relative!r}")
        manifest_outputs.add(relative)
        output = _checked_repo_path(relative)
        if not output.is_file() or sha256_file(output) != entry.get("sha256"):
            raise ContractError(f"generated output digest mismatch: {relative}")
    if "proto/generated/descriptor.pb" not in manifest_outputs:
        raise ContractError("binary protobuf descriptor is missing from codegen outputs")
    actual_go_outputs = {generated.relative_to(ROOT).as_posix() for generated in (ROOT / "go" / "internal" / "protocol").rglob("*.pb.go")}
    manifest_go_outputs = {relative for relative in manifest_outputs if relative.endswith(".pb.go")}
    if actual_go_outputs != manifest_go_outputs:
        raise ContractError("generated Go binding file set drifted")
    return data


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
        path = _checked_repo_path(rel)
        if not path.is_file():
            raise ContractError(f"corpus file missing: {rel}")
        digest = sha256_file(path)
        if digest != item.get("sha256"):
            raise ContractError(f"corpus digest mismatch for {corpus_id}: {digest}")
        if item.get("sensitivity") not in {"public", "redacted"}:
            raise ContractError(f"{corpus_id} sensitivity must be public or redacted")
    return manifest


def validate_corpora(manifest_paths: tuple[Path, ...] = CORPUS_MANIFESTS) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    for index, path in enumerate(manifest_paths):
        manifest = validate_corpus(path)
        if index > 0 and not str(manifest.get("compatibility_reason") or "").strip():
            raise ContractError(f"additive corpus manifest must explain compatibility: {path}")
        manifests.append(manifest)
    return manifests


def validate_toolchain(path: Path = TOOLCHAIN_PATH) -> dict[str, Any]:
    data = _load_json(path)
    if data.get("schema_version") != 1 or data.get("status") != "pinned":
        raise ContractError("toolchain lock must be pinned schema_version 1")
    go_raw = data.get("go")
    protoc_raw = data.get("protoc")
    go: dict[str, Any] = go_raw if isinstance(go_raw, dict) else {}
    protoc: dict[str, Any] = protoc_raw if isinstance(protoc_raw, dict) else {}
    if go.get("version") != "1.27.1":
        raise ContractError("toolchain must pin Go 1.27.1")
    if go.get("source") != "https://go.dev/doc/devel/release":
        raise ContractError("toolchain Go source identity is invalid")
    expected_go_artifacts = {
        "windows_amd64": {
            "filename": "go1.27.1.windows-amd64.zip",
            "sha256": "a3911b5e0e1b1053f25ed0675f4c1c6aad1e2bfcf253df2b9be4caabd2edd95d",
        },
        "linux_amd64": {
            "filename": "go1.27.1.linux-amd64.tar.gz",
            "sha256": "63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445",
        },
    }
    if go.get("artifacts") != expected_go_artifacts:
        raise ContractError("toolchain Go artifact identities or SHA-256 digests are invalid")
    if protoc.get("version") != "36.1":
        raise ContractError("toolchain must pin protoc 36.1")
    if protoc.get("release") != "v36.1":
        raise ContractError("toolchain protoc release identity is invalid")
    if protoc.get("source") != "https://github.com/protocolbuffers/protobuf/releases/tag/v36.1":
        raise ContractError("toolchain protoc source identity is invalid")
    if protoc.get("syntax") != "proto3":
        raise ContractError("toolchain syntax must remain proto3")
    expected_protoc_artifacts = {
        "windows_amd64": {
            "filename": "protoc-36.1-win64.zip",
            "sha256": "390e515cb456e6a978553bdb57baf087b054885077fd6da7f7ff0160279c07d6",
        },
        "linux_amd64": {
            "filename": "protoc-36.1-linux-x86_64.zip",
            "sha256": "c4bc672d9d49214dc8cafdceadf4df92182d6ca8e3ec65a56b2d7de5602669b4",
        },
    }
    if protoc.get("artifacts") != expected_protoc_artifacts:
        raise ContractError("toolchain protoc artifact identities or SHA-256 digests are invalid")
    generators_raw = data.get("generators")
    generators: dict[str, Any] = generators_raw if isinstance(generators_raw, dict) else {}
    required_generators = {
        "protoc_gen_go": {
            "module": "google.golang.org/protobuf/cmd/protoc-gen-go",
            "version": "1.36.11",
        },
        "protoc_gen_go_grpc": {
            "module": "google.golang.org/grpc/cmd/protoc-gen-go-grpc",
            "version": "1.6.2",
        },
        "prost": {"crate": "prost", "version": "0.13.5"},
        "tonic_build": {"crate": "tonic-build", "version": "0.12.3"},
        "protox": {"crate": "protox", "version": "0.8.0"},
    }
    if generators != required_generators:
        raise ContractError("toolchain generator identities or versions are invalid")
    required_validators = {
        "python_protobuf": {
            "distribution": "protobuf",
            "version": PINNED_PYTHON_PROTOBUF_VERSION,
            "requires_python": ">=3.10",
            "wheel": {
                "filename": "protobuf-7.35.1-py3-none-any.whl",
                "url": (
                    "https://files.pythonhosted.org/packages/19/c7/"
                    "5f7c636ec43e0c545e28d1f1db71990108306f7bdcb89f069ba97e428e7f/"
                    "protobuf-7.35.1-py3-none-any.whl"
                ),
                "sha256": "4bc97768d8fe4ad6743c8a19403e314511ed9f6d13205b687e52421c023ac1b9",
            },
        }
    }
    if data.get("validators") != required_validators:
        raise ContractError("toolchain Protobuf validator identity or wheel digest is invalid")
    ci = data.get("ci")
    if not isinstance(ci, dict) or ci.get("go_version") != "1.27.1":
        raise ContractError("toolchain CI must pin Go 1.27.1")
    required_gates = {
        "gofmt",
        "go vet",
        "go test",
        "go test -race",
        "python scripts/native_codegen.py --check",
        "python scripts/native_runtime_contract.py --check",
        "cargo fmt/clippy/test for new crates",
    }
    if set(ci.get("gates") or []) != required_gates:
        raise ContractError("toolchain CI gate inventory is invalid")
    return data


def validate_toolchain_consumers() -> None:
    go_mod = (ROOT / "go" / "go.mod").read_text(encoding="utf-8")
    required_go_mod = (
        r"(?m)^go 1\.27$",
        r"(?m)^toolchain go1\.27\.1$",
        r"(?m)^\s*google\.golang\.org/grpc v1\.83\.0$",
        r"(?m)^\s*google\.golang\.org/protobuf v1\.36\.11$",
    )
    if any(re.search(pattern, go_mod) is None for pattern in required_go_mod):
        raise ContractError("go.mod drifted from the native toolchain lock")

    protobuf_requirement = (ROOT / "requirements-native-protocol.txt").read_text(encoding="utf-8").strip()
    expected_protobuf_requirement = (
        "protobuf @ https://files.pythonhosted.org/packages/19/c7/"
        "5f7c636ec43e0c545e28d1f1db71990108306f7bdcb89f069ba97e428e7f/"
        "protobuf-7.35.1-py3-none-any.whl"
        "#sha256=4bc97768d8fe4ad6743c8a19403e314511ed9f6d13205b687e52421c023ac1b9"
    )
    if protobuf_requirement != expected_protobuf_requirement:
        raise ContractError("Python Protobuf descriptor validator drifted from the toolchain lock")
    requirements_dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    if re.search(r"(?m)^-r requirements-native-protocol\.txt$", requirements_dev) is None:
        raise ContractError("development requirements omit the Protobuf descriptor validator")

    cargo = (ROOT / "rust" / "Cargo.toml").read_text(encoding="utf-8")
    for dependency, version in (("prost", "0.13.5"), ("tonic", "0.12.3"), ("protox", "0.8.0"), ("tonic-build", "0.12.3")):
        if f'{dependency} = "={version}"' not in cargo:
            raise ContractError(f"Cargo workspace dependency {dependency} drifted from the native toolchain lock")

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    go_url = "https://go.dev/dl/go1.27.1.linux-amd64.tar.gz"
    go_sha = "63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445"
    protoc_url = "https://github.com/protocolbuffers/protobuf/releases/download/v36.1/protoc-36.1-linux-x86_64.zip"
    protoc_sha = "c4bc672d9d49214dc8cafdceadf4df92182d6ca8e3ec65a56b2d7de5602669b4"
    if workflow.count(go_url) < 2 or workflow.count(go_sha) < 2:
        raise ContractError("native CI does not install checksum-pinned Go in every native job")
    if protoc_url not in workflow or protoc_sha not in workflow:
        raise ContractError("native CI does not install checksum-pinned protoc")
    if workflow.count("GOTOOLCHAIN: local") < 2 or "actions/setup-go@" in workflow:
        raise ContractError("native CI must disable implicit Go toolchain downloads")
    if "--require-hashes" not in workflow or "-r requirements-native-protocol.txt" not in workflow:
        raise ContractError("native protocol CI must install the checksum-pinned Protobuf validator")
    if "cargo clippy --locked" not in workflow or "cargo test --locked" not in workflow:
        raise ContractError("native Rust CI must consume Cargo.lock fail-closed")
    rust_coverage = (ROOT / "scripts" / "run_rust_coverage.py").read_text(encoding="utf-8")
    if rust_coverage.count('"--locked"') < 3 or "cargo test --locked --manifest-path" not in rust_coverage:
        raise ContractError("Rust coverage evidence must consume Cargo.lock fail-closed")


def validate_command_codes(path: Path = COMMAND_CODES_PATH) -> dict[str, Any]:
    data = _load_json(path)
    codes = data.get("codes")
    if not isinstance(codes, list) or not codes:
        raise ContractError("command codes must be a non-empty list")
    go = (ROOT / "go/internal/protocol/fence.go").read_text(encoding="utf-8")
    rust = (ROOT / "rust/crates/deepseek-protocol/src/lib.rs").read_text(encoding="utf-8")
    for code in codes:
        name = str(code)
        if name not in go or name not in rust:
            raise ContractError(f"command code {name} missing from Go or Rust protocol")
    commands = data.get("commands")
    if not isinstance(commands, dict) or not commands:
        raise ContractError("command map must be a non-empty object")
    for name, code in commands.items():
        if str(code) not in codes:
            raise ContractError(f"command {name} uses unknown code {code}")
    action = (ROOT / "go/internal/action/action.go").read_text(encoding="utf-8")
    production_code = str(data.get("production_code") or "")
    if production_code != "MUTATION_DENIED":
        raise ContractError("production_code must be MUTATION_DENIED")
    if production_code not in go:
        raise ContractError("production_code missing from Go protocol")
    executes = data.get("production_execute")
    if not isinstance(executes, list) or not executes:
        raise ContractError("production_execute must be a non-empty list")
    for name in executes:
        if f"func {name}(" not in action:
            raise ContractError(f"missing production execute {name}")
        if "DenyMutation()" not in action:
            raise ContractError("production execute path must deny mutation")
    if "func Dispatch(" not in action or "PlanNative(" not in action:
        raise ContractError("native dispatch must call PlanNative")
    if "func VerifyProof(" not in action:
        raise ContractError("missing VerifyProof")
    crates = data.get("rust_crates")
    if not isinstance(crates, list) or not crates:
        raise ContractError("rust_crates must be a non-empty list")
    cargo = (ROOT / "rust/Cargo.toml").read_text(encoding="utf-8")
    worker = (ROOT / "rust/crates/deepseek-worker/src/lib.rs").read_text(encoding="utf-8")
    for crate in crates:
        name = str(crate)
        if f"crates/{name}" not in cargo:
            raise ContractError(f"missing rust workspace crate {name}")
        if not (ROOT / "rust/crates" / name / "src/lib.rs").is_file():
            raise ContractError(f"missing rust crate sources {name}")
    if "fn execute(" not in worker or "fn verify_proof(" not in worker:
        raise ContractError("rust worker must expose execute and verify_proof")
    return data


def check_all() -> dict[str, Any]:
    ownership = load_ownership()
    validate_ownership(ownership)
    toolchain = validate_toolchain()
    validate_toolchain_consumers()
    descriptor = check_descriptor()
    codegen = validate_codegen_manifest()
    corpora = validate_corpora()
    command_codes = validate_command_codes()
    return {
        "ok": True,
        "domains": len(ownership["domains"]),
        "proto_files": len(descriptor["files"]),
        "generated_outputs": len(codegen["outputs"]),
        "corpora": sum(len(corpus["corpora"]) for corpus in corpora),
        "corpus_versions": len(corpora),
        "command_codes": len(command_codes["codes"]),
        "go": toolchain["go"]["version"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native runtime contract freeze checks")
    parser.add_argument("--check", action="store_true")
    parser.parse_args(argv)
    try:
        report = check_all()
    except ContractError as exc:
        print(f"native runtime contract FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
