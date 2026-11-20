"""Freeze actual original HTTP outcomes independently from native rendering."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from deepseek_infra.infra.media import library
from tests.test_web_media_routes import _auth_headers, _request, _running_server

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/native_media_retained_http_v1.json"


def fixture() -> dict[str, Any]:
    def invalid_constant(value: str) -> None:
        raise AssertionError(value)

    return json.loads(FIXTURE.read_text(encoding="utf8"), parse_constant=invalid_constant)


def test_retained_http_fixture_keeps_complete_original_case_and_source_identity() -> None:
    data = fixture()
    assert data["schemaVersion"] == 1
    cases = data["cases"]
    assert len(cases) == len({case["name"] for case in cases}) == 34
    assert sum(case["results"]["list"]["status"] == 200 for case in cases) == 18
    assert sum(case["results"]["list"]["status"] == 500 for case in cases) == 16
    provenance = data["provenance"]
    for relative, key in [
        ("deepseek_infra/infra/media/library.py", "originalReaderSha256"),
        ("deepseek_infra/web/server.py", "originalServerSha256"),
        ("deepseek_infra/web/http_utils.py", "originalHttpUtilsSha256"),
    ]:
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == provenance[key]
    assert provenance["actualSocketRequests"] == 170
    assert provenance["nativeComparisonExecuted"] is False
    assert provenance["allSupportedOriginalInterpretersQualified"] is False
    for case in cases:
        assert re.fullmatch(r"[a-z0-9_]+", case["name"])
        assert hashlib.sha256(case["document"].encode("utf8")).hexdigest() == case["documentSha256"]
        assert set(case["results"]) == {"list", "detail", "independentDetail", "filteredList", "unauthorized"}
        for role in ["independentDetail", "filteredList"]:
            assert case["results"][role]["status"] == 200
        assert case["results"]["unauthorized"]["status"] == 401
        for result in case["results"].values():
            assert json.loads(result["bodyJson"]) == result["body"]


def test_original_http_retained_fixture_replays_actual_socket_routes(tmp_settings: Path) -> None:
    assert library.store_path().resolve().is_relative_to(tmp_settings.resolve())
    library.media_dir().mkdir(parents=True, exist_ok=True)
    paths = {"list": "/api/media", "detail": "/api/media/media_kept",
             "independentDetail": "/api/media/media_control", "filteredList": "/api/media?type=pdf",
             "unauthorized": "/api/media"}
    requests = 0
    with _running_server() as server:
        for case in fixture()["cases"]:
            raw = case["document"].encode("utf8")
            library.store_path().write_bytes(raw)
            # Preserve the frozen legal native cases without inventing one
            # universal recursion boundary for all original Python versions.
            decode_recursion_error = False
            try:
                json.loads(case["document"])
            except RecursionError:
                assert case["name"] in {"metadata_sanitized_away_depth_2048", "source_sanitized_away_depth_2048"}
                decode_recursion_error = True
            for role, uri in paths.items():
                headers = {} if role == "unauthorized" else _auth_headers()
                status, body, response = _request(server, "GET", uri, headers=headers)
                assert response.getheader("Content-Type", "").startswith("application/json")
                expected = case["results"][role]
                if decode_recursion_error and role != "unauthorized":
                    expected = {"status": 500, "body": {"error": "Server error", "code": "internal"}}
                assert status == expected["status"], (case["name"], role)
                assert json.loads(body) == expected["body"], (case["name"], role)
                assert library.store_path().read_bytes() == raw
                requests += 1
    assert requests == 170


def test_every_original_case_has_a_distinct_native_and_windows_regression() -> None:
    native = (ROOT / "rust/crates/deepseek-gateway/src/media_retained_http_tests.rs").read_text(encoding="utf8")
    windows = (ROOT / ".github/workflows/native-windows-cloud.yml").read_text(encoding="utf8")
    names = re.findall(r"#\[tokio::test\]\s+async fn (retained_http_[a-z0-9_]+)\(\)", native)
    assert set(names) == {"retained_http_" + case["name"] for case in fixture()["cases"]}
    assert len(names) == 34
    names.append("retained_http_comparison_preserves_integers_and_scalar_types")
    for name in names:
        assert windows.count("'media_segment_routes::media_read_spool::tests::" + name + "'") == 1
