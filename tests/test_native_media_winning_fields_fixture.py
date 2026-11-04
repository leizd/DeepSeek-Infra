"""Bind native JSON regression goldens to actual original Python readers."""
import hashlib
import json
from pathlib import Path

import pytest

from deepseek_infra.infra.media import library

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/native_media_winning_fields_v1.json").read_text(encoding="utf-8"))


def test_python_document_fixture_is_bound_to_original_reader() -> None:
    assert FIXTURE["schemaVersion"] == 1 and FIXTURE["caseCount"] == 10
    assert FIXTURE["originalReaderSha256"] == hashlib.sha256(Path(library.__file__).read_bytes()).hexdigest()
    assert len({case["name"] for case in FIXTURE["cases"]}) == 10
    assert sum(bool(case["publicRows"]) for case in FIXTURE["cases"]) == 10


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_python_document_fixture_matches_original(case: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "library.json"
    monkeypatch.setattr(library, "store_path", lambda: path)
    source = case["document"].encode("utf-8")
    path.write_bytes(source)
    assert hashlib.sha256(source).hexdigest() == case["documentSha256"]
    assert library._load_store() == case["storedRows"]
    assert library.list_media() == case["publicRows"]
    assert case["readerError"] is None and case["publicUtf8Serializable"] is True
    assert hashlib.sha256(json.dumps(case["publicRows"], ensure_ascii=False).encode("utf-8")).hexdigest() == case["bodySha256"]
    assert path.read_bytes() == source


def test_original_clock_precedes_invalid_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch
    from deepseek_infra.infra.media import schema
    case = FIXTURE["clock"]
    assert FIXTURE["originalSchemaSha256"] == hashlib.sha256(Path(schema.__file__).read_bytes()).hexdigest()
    path = tmp_path / "library.json"
    path.write_text(case["document"], encoding="utf-8")
    monkeypatch.setattr(library, "store_path", lambda: path)
    for reader in [library._load_store, library.list_media]:
        with patch.object(schema, "utc_now", side_effect=["clock-first", "clock-second", "clock-third"]) as clock:
            assert reader() == case["rows"]
            assert clock.call_count == 2
    assert hashlib.sha256(path.read_bytes()).hexdigest() == case["documentSha256"]
