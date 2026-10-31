"""Original processing references and fail-closed cloud observer checks."""
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_original_processing_reference_exercises_all_six_types_index_search_and_force(tmp_path):
    processing = importlib.import_module("verify_native_media_processing")
    result = processing.original(tmp_path / "original", tmp_path / "original.log", "sqlite")
    processing.require_reference(result, "sqlite")
    assert {row["type"] for row in result["cases"]} == {"webpage", "image", "screenshot", "pdf", "audio", "video"}
    assert all(row["forceStatus"] == 200 and row["publicReadsMatch"] and row["search"] for row in result["cases"])
    assert result["embeddingProvider"] == "hash"


def test_processing_observer_never_starts_native_workloads_locally(tmp_path, monkeypatch):
    processing = importlib.import_module("verify_native_media_processing")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(processing.create.ProbeFailure, match="CLOUD_ONLY"):
        processing.verify("http://127.0.0.1:1", "", tmp_path, tmp_path, tmp_path, {})


@pytest.mark.parametrize("reference", [
    {"status": "PASS", "backend": "sqlite", "cases": []},
    {"status": "FAIL", "backend": "sqlite", "cases": []},
    {"status": "PASS", "backend": "sqlite", "embeddingProvider": "hash", "cases": None},
    {"status": "PASS", "backend": "sqlite_vec", "vectorTableAvailable": False, "cases": []},
])
def test_processing_observer_refuses_partial_or_unavailable_original_references(reference):
    processing = importlib.import_module("verify_native_media_processing")
    with pytest.raises(processing.create.ProbeFailure):
        processing.require_reference(reference, reference["backend"])
