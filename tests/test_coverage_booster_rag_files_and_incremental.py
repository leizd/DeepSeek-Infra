"""Targeted test coverage boosters for rag/files and backup_incremental."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.rag import files
from deepseek_infra.infra.workspace import backup_incremental


def test_rag_files_helpers(tmp_settings: Path) -> None:
    # 1. File type detection
    assert files.is_image_file(".png", "image/png") is True
    assert files.is_image_file(".txt", "text/plain") is False

    assert files.is_text_file(".py", "text/x-python", b"print('hi')") is True
    assert files.is_text_file(".bin", "application/octet-stream", b"\x00\x01\x02") is False

    # 2. Text file decoding and html extraction
    assert files.decode_text_file(b"Hello world") == "Hello world"
    html_sample = b"<html><head><title>Test</title></head><body><h1>Heading</h1><p>Paragraph text</p></body></html>"
    extracted_html = files.extract_html_text(html_sample)
    assert "Heading" in extracted_html
    assert "Paragraph text" in extracted_html

    # 3. Query classification and scoring
    assert files.is_broad_file_query("总结一下这篇文档") is True
    assert files.is_broad_file_query("x") is False

    score = files.hybrid_chunk_score({"text": "DeepSeek infrastructure"}, "DeepSeek infrastructure", ["deepseek"], "deepseek")
    assert score > 0

    # 4. Chunk locator formatting
    loc = files.format_chunk_locator({}, 0, 5, 0, 100)
    assert "0/5" in loc

    # 5. Project file cache directory
    p_dir = files.project_file_cache_dir("proj_123")
    assert "proj_123" in str(p_dir) or "projects" in str(p_dir)

    # 6. Reader validations
    assert files._reader_positive_int(10, "bad int", default=1) == 10
    assert files._reader_positive_int(None, "bad int", default=5) == 5
    with pytest.raises(AppError):
        files._reader_positive_int("not_a_number", "bad int", default=1)

    assert files._reader_scale_float(2.0, "bad float", default=1.0) == 2.0
    assert files._reader_scale_float(None, "bad float", default=1.5) == 1.5
    with pytest.raises(AppError):
        files._reader_scale_float("not_a_float", "bad float", default=1.0)

    # 7. Normalized page texts
    page_texts = [{"page": 1, "text": "Page 1 content"}, {"page": 2, "text": "Page 2 content"}]
    norm = files.normalized_page_texts(page_texts)
    assert len(norm) == 2
    assert files.page_text_for_index(norm, 1) == "Page 1 content"
    assert files.page_text_for_index(norm, 99) == ""


def test_backup_incremental_helpers(tmp_settings: Path) -> None:
    # 1. Merkle leaves and tree computation
    leaf1 = backup_incremental.leaf_digest(contributor_id="memory", logical_path="state.json", size=10, sha256="a" * 64)
    leaf2 = backup_incremental.leaf_digest(contributor_id="workspace", logical_path="file.txt", size=20, sha256="b" * 64)
    assert len(leaf1) == 64
    assert len(leaf2) == 64

    tree_root = backup_incremental.merkle_root([leaf1, leaf2])
    assert len(tree_root) == 64
    assert len(backup_incremental.merkle_root([])) == 64

    # 2. File version ID and chunk map ID
    fv_id = backup_incremental.file_version_id(size=100, sha256="c" * 64, chunk_map_id="map_123")
    assert len(fv_id) == 64

    cmap_id = backup_incremental.chunk_map_id(protocol="fastcdc-gear-v3", file_size=1024, file_sha256="d" * 64)
    assert len(cmap_id) == 64

    # 3. Scope and recipient digests
    s_dig = backup_incremental.scope_digest({"targets": ["t1"], "contributors": ["c1"]})
    assert len(s_dig) == 64

    r_dig = backup_incremental.recipient_set_digest({"recipients": ["rec1"]})
    assert len(r_dig) == 64

    sch_dig = backup_incremental.schema_digest({"memory": 1, "workspace": 2})
    assert len(sch_dig) == 64

    # 4. Index health markers
    target_id = "target_health_test"
    policy_id = "policy_health_test"
    assert backup_incremental.index_is_healthy(target_id, policy_id) is True

    backup_incremental.mark_index_stale(target_id, policy_id, reason="Testing stale marker")
    assert backup_incremental.index_is_healthy(target_id, policy_id) is False
