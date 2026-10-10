from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.media import library, schema


def test_media_schema_mime_type_and_upload_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    assert schema.media_type_from_mime("image/png; charset=x") == "image"
    assert schema.media_type_from_mime("", filename="paper.pdf") == "pdf"
    assert schema.media_type_from_mime("", filename="voice.m4a") == "audio"
    assert schema.media_type_from_mime("", filename="movie.mkv") == "video"
    assert schema.media_type_from_mime("", filename="page.htm") == "webpage"
    assert schema.media_type_from_mime("application/octet-stream") == ""
    assert schema.normalize_media_type("unknown", mime_type="image/png") == "image"
    with pytest.raises(AppError):
        schema.normalize_media_type("unknown")
    assert schema.validate_media_mime_type("", filename="photo.png") == "image/png"
    with pytest.raises(AppError):
        schema.validate_media_mime_type("application/exe")
    with pytest.raises(AppError):
        schema.validate_media_upload_size(0)
    monkeypatch.setattr(schema, "MAX_MEDIA_UPLOAD_BYTES", 1)
    with pytest.raises(AppError):
        schema.validate_media_upload_size(2)


def test_media_schema_metadata_paths_segments_and_ranges() -> None:
    metadata = schema.normalize_metadata({"bad key!": "x", "nested": {"": "drop", "ok": 1}, "list": [{"x": 1}, None, object()]})
    assert metadata["badkey"] == "x"
    assert metadata["nested"] == {"ok": 1}
    assert metadata["list"][0] == {"x": 1}
    assert schema.normalize_metadata([]) == {}
    for path in ("/absolute", "//server/share", "C:/absolute", "objects/../escape"):
        with pytest.raises(AppError):
            schema.normalize_media_path(path)
    assert schema.normalize_media_path("objects\\media_x\\source.png") == "objects/media_x/source.png"
    with pytest.raises(AppError):
        schema.normalize_segment({"type": "unknown"}, media_id="media_1234")
    segment = schema.normalize_segment(
        {
            "type": "frame",
            "text": "api_key=secretvalue",
            "confidence": 2,
            "page": 0,
            "timeRange": [-2, 1],
            "framePath": "frames/f.jpg",
            "citation": {"uri": "media://x"},
        },
        media_id="media_1234",
        fallback_index=3,
    )
    assert segment["confidence"] == 1.0
    assert segment["page"] == 1
    assert segment["timeRange"] == [0.0, 1.0]
    assert "secretvalue" not in segment["text"]
    assert schema.normalize_confidence("bad") == 1.0
    assert schema.normalize_time_range("bad") == []
    assert schema.normalize_time_range(["bad", 1]) == []


def test_media_library_path_guards_and_empty_path(tmp_settings: Path) -> None:
    media_id = "media_1234"
    assert library.media_file_path({"mediaId": media_id, "path": ""}) == library.object_dir(media_id)
    with pytest.raises(AppError):
        library.relative_media_path(Path.home() / "outside.png")
    assert library.validate_object_media_path("") == ""
    with pytest.raises(AppError):
        library.validate_object_media_path("uploads/source.png")
    assert library.validate_object_media_path(f"objects/{media_id}/source.png", media_id=media_id) == f"objects/{media_id}/source.png"


def test_media_library_filters_updates_and_missing_records(tmp_settings: Path) -> None:
    first = library.register_media(media_id="media_first", project_id="proj_1234", media_type="image", title="One", status="pending")
    second = library.register_media(media_id="media_second", media_type="pdf", title="Two", status="ready")
    assert [item["mediaId"] for item in library.list_media(project_id="proj_1234")] == [first["mediaId"]]
    assert [item["mediaId"] for item in library.list_media(media_type="pdf", status="ready")] == [second["mediaId"]]
    updated = library.update_media(
        first["mediaId"],
        {"title": "Changed", "metadata": "bad", "type": "screenshot", "mimeType": "image/png", "source": {"kind": "test"}},
    )
    assert updated["title"] == "Changed" and updated["metadata"] == {}
    with pytest.raises(AppError):
        library.get_media("media_missing")
    with pytest.raises(AppError):
        library.update_media("media_missing", {})
    assert library.delete_media("media_missing") == 0


def test_media_source_atomic_write_and_text_source(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_id = "media_write"
    path = library.save_source_bytes(media_id, "photo.PNG", b"data")
    assert path.endswith("objects/media_write/source.png")
    assert not (library.object_dir(media_id) / "source.png.tmp").exists()
    text_path = library.save_text_source(media_id, "notes.txt", "hello")
    assert library.media_file_path({"mediaId": media_id, "path": text_path}).read_text(encoding="utf-8") == "hello"
    with pytest.raises(AppError):
        library.save_source_bytes(media_id, "empty.bin", b"")


def test_media_segments_skip_corruption_and_invalid_rows(tmp_settings: Path) -> None:
    media_id = "media_segments"
    path = library.segments_path(media_id)
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    assert library.list_segments(media_id) == []
    path.write_text("[]", encoding="utf-8")
    assert library.list_segments(media_id) == []
    path.write_text(
        json.dumps({"segments": [None, {"type": "bad"}, {"type": "caption", "text": "ok", "segmentId": "seg_valid"}]}),
        encoding="utf-8",
    )
    result = library.list_segments(media_id)
    assert len(result) == 1 and result[0]["text"] == "ok"


def test_media_store_skips_invalid_records_and_non_list(tmp_settings: Path) -> None:
    path = library.store_path()
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    assert library._load_store() == []
    path.write_text("[]", encoding="utf-8")
    assert library._load_store() == []
    path.write_text(json.dumps({"media": "bad"}), encoding="utf-8")
    assert library._load_store() == []
    path.write_text(json.dumps({"media": [None, {"mediaId": "bad"}, {"mediaId": "media_valid", "type": "image", "title": "ok"}]}), encoding="utf-8")
    assert [item["mediaId"] for item in library._load_store()] == ["media_valid"]


def test_delete_media_contains_cleanup_failures(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = library.register_media(media_id="media_delete", media_type="image", title="Delete")
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(library.shutil, "rmtree", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    assert library.delete_media(media["mediaId"]) == 1
