from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import google.protobuf
from google.protobuf import descriptor_pb2

from scripts.native_runtime_contract import (
    BINARY_DESCRIPTOR_PATH,
    CODEGEN_MANIFEST_PATH,
    DESCRIPTOR_PATH,
    ContractError,
    check_descriptor,
    descriptor_set_to_contract,
    load_binary_descriptor,
    validate_codegen_manifest,
    validate_descriptor_compatibility,
    validate_descriptor_invariants,
    validate_toolchain,
)


ROOT = Path(__file__).resolve().parents[1]


def _binary_contract() -> tuple[Any, dict[str, Any]]:
    descriptor_set = load_binary_descriptor()
    return descriptor_set, descriptor_set_to_contract(descriptor_set)


def test_proto_sources_are_proto3_with_unspecified_zero_enums() -> None:
    _, descriptor = _binary_contract()
    packages = {item["package"] for item in descriptor["files"]}
    assert packages == {
        "deepseek.common.v1",
        "deepseek.action.v1",
        "deepseek.storage.v1",
        "deepseek.federation.v1",
        "deepseek.control.v1",
        "deepseek.evidence.v1",
        "deepseek.agent.v1",
        "deepseek.browser.v1",
        "deepseek.platform.v1",
    }
    checked = check_descriptor()
    assert DESCRIPTOR_PATH.is_file()
    assert BINARY_DESCRIPTOR_PATH.is_file()
    assert checked["syntax"] == "proto3"


def test_platform_decoder_has_only_streamed_value_operations() -> None:
    descriptors, _ = _binary_contract()
    platform = next(item for item in descriptors.file if item.package == "deepseek.platform.v1")
    assert platform.name == "platform/v1/ocr.proto"
    assert len(platform.service) == 1
    methods = platform.service[0].method
    assert [item.name for item in methods] == ["RecognizeImage", "RecognizePdf", "RenderPdfPage"]
    assert all(item.client_streaming and not item.server_streaming for item in methods)
    assert all(item.input_type == ".deepseek.platform.v1.DocumentChunk" for item in methods)
    assert [item.output_type for item in methods] == [
        ".deepseek.platform.v1.RecognizedDocument", ".deepseek.platform.v1.RecognizedDocument",
        ".deepseek.platform.v1.RenderedPdfPage",
    ]
    header = next(item for item in platform.message_type if item.name == "DocumentHeader")
    assert [(item.name, item.number) for item in header.field] == [
        ("source_bytes", 1), ("source_sha256", 2), ("page_index", 3), ("scale_milli", 4),
    ]


def test_action_fence_and_unknown_effect_are_foundational() -> None:
    _, descriptor = _binary_contract()
    common = next(item for item in descriptor["files"] if item["package"] == "deepseek.common.v1")
    fence = next(item for item in common["messages"] if item["name"] == "ActionFence")
    assert [field["name"] for field in fence["fields"]] == ["action_id", "execution_epoch"]
    effect = next(item for item in common["enums"] if item["name"] == "EffectState")
    assert effect["values"][0]["name"] == "EFFECT_STATE_UNSPECIFIED"
    assert effect["values"][0]["number"] == 0
    assert any(item["name"] == "EFFECT_STATE_UNKNOWN" for item in effect["values"])


def test_control_plane_has_no_mutation_rpc() -> None:
    _, descriptor = _binary_contract()
    control = next(item for item in descriptor["files"] if item["package"] == "deepseek.control.v1")
    rpcs = [rpc["name"] for service in control["services"] for rpc in service["rpcs"]]
    assert rpcs == ["Health", "ShadowEvaluate", "GetBackupPolicyRecipients"]


@pytest.mark.parametrize("tamper", ["request-field", "response-field", "group-field", "streaming", "wrong-response"])
def test_recipient_read_rpc_cannot_smuggle_a_mutation_shape(tamper: str) -> None:
    descriptor_set = load_binary_descriptor()
    control = next(item for item in descriptor_set.file if item.package == "deepseek.control.v1")
    service = next(item for item in control.service if item.name == "ControlPlane")
    rpc = next(item for item in service.method if item.name == "GetBackupPolicyRecipients")
    if tamper == "streaming":
        rpc.client_streaming = True
    elif tamper == "wrong-response":
        rpc.output_type = ".deepseek.control.v1.HealthResponse"
    else:
        name = {
            "request-field": "BackupPolicyRecipientsRequest",
            "response-field": "BackupPolicyRecipientsResponse",
            "group-field": "BackupRecipientGroup",
        }[tamper]
        message = next(item for item in control.message_type if item.name == name)
        message.field.add(name="mutation_body", number=99, type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES)
    with pytest.raises(ContractError, match="control proto cannot expose mutation RPCs"):
        validate_descriptor_invariants(descriptor_set_to_contract(descriptor_set))


def test_binary_descriptor_does_not_hide_streaming_or_optioned_rpcs() -> None:
    descriptor_set = load_binary_descriptor()
    control = next(item for item in descriptor_set.file if item.package == "deepseek.control.v1")
    service = next(item for item in control.service if item.name == "ControlPlane")
    rpc = service.method.add(
        name="Mutate",
        input_type=".deepseek.control.v1.HealthRequest",
        output_type=".deepseek.control.v1.HealthResponse",
        client_streaming=True,
        server_streaming=True,
    )
    rpc.options.deprecated = True

    with pytest.raises(ContractError, match="control proto cannot expose mutation RPCs"):
        validate_descriptor_invariants(descriptor_set_to_contract(descriptor_set))


def test_frozen_v1_descriptor_rejects_breaking_field_and_rpc_changes() -> None:
    baseline = {
        "schema_version": 1,
        "syntax": "proto3",
        "files": [
            {
                "path": "proto/example/v1/example.proto",
                "package": "deepseek.example.v1",
                "imports": [],
                "messages": [
                    {
                        "name": "Request",
                        "fields": [{"label": "singular", "type": "string", "name": "action_id", "number": 1}],
                    }
                ],
                "enums": [],
                "services": [
                    {
                        "name": "Example",
                        "rpcs": [
                            {
                                "name": "Get",
                                "request": "Request",
                                "response": "Request",
                                "client_streaming": False,
                                "server_streaming": False,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    current = json.loads(json.dumps(baseline))
    current["files"][0]["messages"][0]["fields"][0]["number"] = 2
    current["files"][0]["services"][0]["rpcs"][0]["server_streaming"] = True

    with pytest.raises(ContractError, match="breaking protobuf change"):
        validate_descriptor_compatibility(baseline, current)


def test_binary_descriptor_rejects_optioned_secret_field() -> None:
    descriptor_set = load_binary_descriptor()
    common = next(item for item in descriptor_set.file if item.package == "deepseek.common.v1")
    schema_meta = next(item for item in common.message_type if item.name == "SchemaMeta")
    secret = schema_meta.field.add(
        name="private_key",
        number=99,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    secret.options.deprecated = True

    with pytest.raises(ContractError, match="secret-bearing"):
        validate_descriptor_invariants(descriptor_set_to_contract(descriptor_set))


def test_binary_descriptor_enum_zero_must_be_unspecified() -> None:
    descriptor_set = load_binary_descriptor()
    common = next(item for item in descriptor_set.file if item.package == "deepseek.common.v1")
    effect_state = next(item for item in common.enum_type if item.name == "EffectState")
    effect_state.value[0].name = "EFFECT_STATE_DEFAULT"

    with pytest.raises(ContractError, match="UNSPECIFIED"):
        validate_descriptor_invariants(descriptor_set_to_contract(descriptor_set))


def test_frozen_v1_descriptor_rejects_moving_field_into_oneof() -> None:
    baseline = json.loads(DESCRIPTOR_PATH.read_text(encoding="utf-8"))
    descriptor_set = load_binary_descriptor()
    common = next(item for item in descriptor_set.file if item.package == "deepseek.common.v1")
    fence = next(item for item in common.message_type if item.name == "ActionFence")
    fence.oneof_decl.add(name="choice")
    fence.field[0].oneof_index = 0

    with pytest.raises(ContractError, match="breaking protobuf change"):
        validate_descriptor_compatibility(baseline, descriptor_set_to_contract(descriptor_set))


def test_binary_descriptor_rejects_malformed_payload(tmp_path: Path) -> None:
    malformed = tmp_path / "descriptor.pb"
    malformed.write_bytes(b"not-a-file-descriptor-set")

    with pytest.raises(ContractError, match="invalid binary protobuf descriptor"):
        load_binary_descriptor(malformed)


def test_binary_descriptor_requires_exact_validator_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google.protobuf, "__version__", "7.35.1-spoofed")

    with pytest.raises(ContractError, match="validator version mismatch"):
        load_binary_descriptor()


def test_toolchain_pins_go_1_27_1_and_protoc_36() -> None:
    data = validate_toolchain()
    assert data["go"]["version"] == "1.27.1"
    assert data["protoc"]["version"] == "36.1"
    assert data["protoc"]["syntax"] == "proto3"
    assert data["validators"]["python_protobuf"]["version"] == "7.35.1"
    assert data["validators"]["python_protobuf"]["wheel"]["sha256"] == (
        "4bc97768d8fe4ad6743c8a19403e314511ed9f6d13205b687e52421c023ac1b9"
    )


def test_toolchain_validator_rejects_identity_and_artifact_tampering(tmp_path: Path) -> None:
    toolchain = json.loads((ROOT / "release/native_runtime_toolchain_v1.json").read_text(encoding="utf-8"))
    toolchain["schema_version"] = 999
    toolchain["generators"]["protoc_gen_go"]["module"] = "attacker.invalid/generator"
    toolchain["go"].pop("artifacts")
    tampered = tmp_path / "toolchain.json"
    tampered.write_text(json.dumps(toolchain), encoding="utf-8")

    with pytest.raises(ContractError, match="toolchain lock"):
        validate_toolchain(tampered)


def test_toolchain_validator_rejects_protobuf_wheel_tampering(tmp_path: Path) -> None:
    toolchain = json.loads((ROOT / "release/native_runtime_toolchain_v1.json").read_text(encoding="utf-8"))
    toolchain["validators"]["python_protobuf"]["wheel"]["sha256"] = "0" * 64
    tampered = tmp_path / "toolchain.json"
    tampered.write_text(json.dumps(toolchain), encoding="utf-8")

    with pytest.raises(ContractError, match="Protobuf validator"):
        validate_toolchain(tampered)


def test_codegen_manifest_rejects_tampered_generated_output(tmp_path: Path) -> None:
    manifest = json.loads(CODEGEN_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["outputs"][0]["sha256"] = "0" * 64
    tampered = tmp_path / "codegen-manifest.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ContractError, match="generated output digest mismatch"):
        validate_codegen_manifest(tampered)


def test_frozen_descriptor_baseline_cannot_be_rewritten_in_place(tmp_path: Path) -> None:
    baseline = DESCRIPTOR_PATH.read_bytes() + b"\n"
    tampered = tmp_path / "descriptor.v1.json"
    tampered.write_bytes(baseline)

    with pytest.raises(ContractError, match="baseline digest changed"):
        check_descriptor(tampered)


def _bootstrap_binding_contract() -> dict[str, Any]:
    descriptors = load_binary_descriptor()
    action = next(item for item in descriptors.file if item.package == "deepseek.action.v1")
    if not any(item.name == "ControlSignerBindingRequest" for item in action.message_type):
        for name, fields in {
            "ControlSignerBindingRequest": [("schema_version", 13), ("fleet_id", 9), ("environment", 9), ("fencing_token", 4), ("expected_signer_public_key", 9)],
            "ControlSignerBindingResponse": [("schema_version", 13), ("signer_public_key", 9), ("signer_key_id", 9), ("fleet_id", 9), ("environment", 9), ("fencing_token", 4)],
        }.items():
            message = action.message_type.add(name=name)
            for number, (field, kind) in enumerate(fields, 1):
                message.field.add(name=field, number=number, label=1, type=kind)
        service = action.service.add(name="ControlSignerBootstrap")
        service.method.add(name="GetBinding", input_type=".deepseek.action.v1.ControlSignerBindingRequest", output_type=".deepseek.action.v1.ControlSignerBindingResponse")
    return descriptor_set_to_contract(descriptors)


def test_binding_discovery_is_read_only_without_an_action_identity() -> None:
    validate_descriptor_invariants(_bootstrap_binding_contract())


@pytest.mark.parametrize("alteration", ["field", "method", "response", "stream", "options", "extra-method", "extra-field"])
def test_binding_discovery_exception_requires_exact_read_only_shape(alteration: str) -> None:
    contract = _bootstrap_binding_contract()
    action = next(item for item in contract["files"] if item["package"] == "deepseek.action.v1")
    request = next(item for item in action["messages"] if item["name"] == "ControlSignerBindingRequest")
    service = next(item for item in action["services"] if item["name"] == "ControlSignerBootstrap")
    rpc = service["rpcs"][0]
    if alteration == "field":
        request["fields"][0]["type"] = "uint64"
    elif alteration == "method":
        rpc["name"] = "InstallBinding"
    elif alteration == "response":
        rpc["response"] = ".deepseek.action.v1.SignControlResponse"
    elif alteration == "stream":
        rpc["server_streaming"] = True
    elif alteration == "options":
        request["options_hex"] = "1801"
    elif alteration == "extra-method":
        service["rpcs"].append(dict(rpc, name="Write"))
    else:
        request["fields"].append({"name": "canonical_document", "number": 99, "type": "bytes", "label": "singular"})
    with pytest.raises(ContractError, match="ActionFence"):
        validate_descriptor_invariants(contract)
