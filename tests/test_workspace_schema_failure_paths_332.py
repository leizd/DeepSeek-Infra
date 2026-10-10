from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import schema


def test_workspace_schema_timestamp_ids_and_validation_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    assert schema.timestamp_ms_to_iso("bad") == ""
    assert schema.timestamp_ms_to_iso(-1) == ""
    assert schema.timestamp_ms_to_iso(1_000).startswith("1970-01-01T00:00:01")
    monkeypatch.setattr(schema.secrets, "token_hex", lambda _size: "a" * 16)
    assert schema.new_id(" Bad-Prefix! ") == "badprefix_" + "a" * 16
    assert schema.new_id("!!!").startswith("item_")
    for value in ("", "abc", "../project", "x" * 65):
        with pytest.raises(AppError):
            schema.validate_project_id(value)
    for value in ("", "abc", "../saved", "x" * 81):
        with pytest.raises(AppError):
            schema.validate_workspace_id(value, label="saved item id")


def test_workspace_schema_normalizes_nested_untrusted_values() -> None:
    assert schema.normalize_title("  hello\n world  ") == "hello world"
    assert schema.normalize_title("", default="Fallback") == "Fallback"
    assert schema.normalize_description("a\r\nb") == "a\nb"
    assert schema.normalize_content(" a\r\nb ") == "a\nb"
    tags = schema.normalize_tags([" One ", "one", "", None, "Two", *[f"t{i}" for i in range(30)]])
    assert tags[:2] == ["One", "Two"]
    assert len(tags) == schema.MAX_TAGS
    assert schema.normalize_tags("not-a-list") == []

    source = schema.normalize_source_ref(
        {
            "bad key!": "value",
            "nested": {"": "drop", "ok": "yes"},
            "list": [1, None, "x"],
            "none": None,
            "bool": True,
            "float": 1.5,
        }
    )
    assert source == {
        "badkey": "value",
        "nested": {"ok": "yes"},
        "list": ["1", "x"],
        "none": None,
        "bool": True,
        "float": 1.5,
    }
    assert schema.normalize_source_ref([]) == {}


def test_workspace_schema_type_and_format_rejections() -> None:
    assert schema.normalize_saved_type(" TRACE ") == "trace"
    with pytest.raises(AppError):
        schema.normalize_saved_type("credential")
    assert schema.normalize_saved_purpose("unknown") == "reference"
    assert schema.normalize_artifact_type("md") == "markdown"
    assert schema.normalize_artifact_type("", path="report.md") == "markdown"
    with pytest.raises(AppError):
        schema.normalize_artifact_type("exe")
    assert schema.normalize_export_format(".MD") == "markdown"
    with pytest.raises(AppError):
        schema.normalize_export_format("pdf")


def test_workspace_schema_json_atomic_and_filename_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "nested" / "state.json"
    assert schema.read_json_file(target, {"fallback": True}) == {"fallback": True}
    target.parent.mkdir()
    target.write_text("not-json", encoding="utf-8")
    assert schema.read_json_file(target, {"fallback": True}) == {"fallback": True}
    target.write_text("[]", encoding="utf-8")
    assert schema.read_json_file(target, {"fallback": True}) == {"fallback": True}
    schema.write_json_atomic(target, {"ok": True})
    assert schema.read_json_file(target) == {"ok": True}
    assert not target.with_suffix(".json.tmp").exists()
    assert schema.safe_filename(" .. ", "fallback") == "fallback"
    assert schema.safe_filename("hello / world") == "hello-world"

    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")))
    assert schema.read_json_file(target, {"safe": True}) == {"safe": True}


def test_workspace_schema_runtime_paths_stay_inside_runtime_roots(tmp_settings: Path) -> None:
    generated = tmp_settings / ".generated"
    projects = tmp_settings / ".projects"
    generated.mkdir(exist_ok=True)
    projects.mkdir(exist_ok=True)
    generated_file = generated / "nested" / "report.md"
    project_file = projects / "proj_1" / "state.json"
    assert schema.runtime_relative_path(str(generated_file)) == ".generated/nested/report.md"
    assert schema.runtime_relative_path(str(project_file)) == ".projects/proj_1/state.json"
    assert schema.runtime_relative_path("folder\\note.txt") == "folder/note.txt"
    assert schema.resolve_runtime_path(".generated/nested/report.md") == generated_file.resolve()
    assert schema.resolve_runtime_path(".projects/proj_1/state.json") == project_file.resolve()
    assert schema.resolve_runtime_path("README.md") == (schema.config.ROOT / "README.md").resolve()
    for value in ("", "../escape.txt", str(Path.home() / "outside.txt")):
        with pytest.raises(AppError):
            schema.runtime_relative_path(value)


def test_workspace_schema_redacts_secrets_recursively() -> None:
    redacted = schema.redact_value(
        {
            "api_key": "plain",
            "nested": ["Bearer abcdefghijkl", ("api_key=abcdefgh", 3)],
            "object": object(),
            "none": None,
        }
    )
    assert redacted["api_key"] == "[redacted]"
    assert redacted["nested"][0] == "Bearer [redacted]"
    assert redacted["nested"][1][0] == "api_key=[redacted]"
    assert redacted["none"] is None
    assert schema.contains_secret(b"authorization: Bearer abcdefghijkl") is True
    assert schema.contains_secret("https://example.test/?api_key=secretvalue") is True
    assert schema.contains_secret("ordinary text") is False
