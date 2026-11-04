"""Bind native JSON regression goldens to actual original Python readers."""
import hashlib
import json
from pathlib import Path

import pytest

from deepseek_infra.infra.media import library

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/native_media_python_contract_v1.json").read_text(encoding="utf-8"))


def test_python_document_fixture_is_bound_to_original_reader() -> None:
    assert FIXTURE["schemaVersion"] == 1 and FIXTURE["caseCount"] == 328
    assert FIXTURE["originalReaderSha256"] == hashlib.sha256(Path(library.__file__).read_bytes()).hexdigest()
    assert len({case["name"] for case in FIXTURE["cases"]}) == 328
    assert sum(bool(case["publicRows"]) for case in FIXTURE["cases"]) == 312


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
