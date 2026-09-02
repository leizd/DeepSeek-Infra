from __future__ import annotations

from pathlib import Path

import pytest

from scripts.native_runtime_contract import (
    DESCRIPTOR_PATH,
    ContractError,
    check_descriptor,
    collect_protos,
    parse_proto,
    validate_toolchain,
)


ROOT = Path(__file__).resolve().parents[1]


def test_proto_sources_are_proto3_with_unspecified_zero_enums() -> None:
    documents = collect_protos()
    packages = {item["package"] for item in documents}
    assert packages == {
        "deepseek.common.v1",
        "deepseek.action.v1",
        "deepseek.storage.v1",
        "deepseek.federation.v1",
        "deepseek.control.v1",
        "deepseek.evidence.v1",
        "deepseek.agent.v1",
    }
    descriptor = check_descriptor()
    assert DESCRIPTOR_PATH.is_file()
    assert descriptor["syntax"] == "proto3"


def test_action_fence_and_unknown_effect_are_foundational() -> None:
    common = next(item for item in collect_protos() if item["package"] == "deepseek.common.v1")
    fence = next(item for item in common["messages"] if item["name"] == "ActionFence")
    assert [field["name"] for field in fence["fields"]] == ["action_id", "execution_epoch"]
    effect = next(item for item in common["enums"] if item["name"] == "EffectState")
    assert effect["values"][0] == {"name": "EFFECT_STATE_UNSPECIFIED", "number": 0}
    assert any(item["name"] == "EFFECT_STATE_UNKNOWN" for item in effect["values"])


def test_control_plane_has_no_mutation_rpc() -> None:
    control = next(item for item in collect_protos() if item["package"] == "deepseek.control.v1")
    rpcs = [rpc["name"] for service in control["services"] for rpc in service["rpcs"]]
    assert rpcs == ["Health", "ShadowEvaluate"]


def test_secret_field_names_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "evil.proto"
    path.write_text(
        'syntax = "proto3";\npackage evil.v1;\nmessage Key { string private_key = 1; }\n',
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="secret-bearing"):
        parse_proto(path)


def test_enum_zero_must_be_unspecified(tmp_path: Path) -> None:
    path = tmp_path / "enum.proto"
    path.write_text(
        'syntax = "proto3";\npackage evil.v1;\nenum Mode { MODE_SHADOW = 0; }\n',
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="UNSPECIFIED"):
        parse_proto(path)


def test_toolchain_pins_go_1_27_1_and_protoc_36() -> None:
    data = validate_toolchain()
    assert data["go"]["version"] == "1.27.1"
    assert data["protoc"]["version_line"] == "36.x"
    assert data["protoc"]["syntax"] == "proto3"
