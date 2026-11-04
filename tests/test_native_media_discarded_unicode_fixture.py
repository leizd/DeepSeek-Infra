"""Keep the native regression oracle bound to the retained Python reader."""
import hashlib
import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.media import library

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/native_media_discarded_unicode_v1.json").read_text(encoding="utf-8"))


def test_discarded_unicode_fixture_is_bound_to_original_reader() -> None:
    assert FIXTURE["schemaVersion"] == 1
    assert FIXTURE["originalReaderSha256"] == hashlib.sha256(Path(library.__file__).read_bytes()).hexdigest()
    assert len(FIXTURE["cases"]) == 21
    assert sum(bool(case["publicRows"]) for case in FIXTURE["cases"]) == 17


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_discarded_unicode_matches_actual_original_reader(case: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "library.json"
    monkeypatch.setattr(library, "store_path", lambda: path)
    source = case["document"].encode("utf-8")
    path.write_bytes(source)
    assert hashlib.sha256(source).hexdigest() == case["documentSha256"]
    assert library._load_store() == case["storedRows"]
    assert library.list_media() == case["publicRows"]
    if case["publicRows"]:
        assert library.get_media("media_kept") == case["publicRows"][0]
    else:
        with pytest.raises(AppError) as error:
            library.get_media("media_kept")
        assert error.value.status == 404
    json.dumps(case["publicRows"], ensure_ascii=False).encode("utf-8")
    assert path.read_bytes() == source
