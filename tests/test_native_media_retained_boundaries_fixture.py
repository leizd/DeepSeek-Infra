"""Replay the original HTTP distinctions between normalization and encoding."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.media import library
from tests.test_web_media_routes import _auth_headers, _request, _running_server


ROOT = Path(__file__).resolve().parents[1]


def load_wire_probe(monkeypatch: Any) -> Any:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("retained_wire_probe", ROOT / "scripts/verify_native_media_reads.py")
    assert spec and spec.loader
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    return probe


def test_original_retained_boundary_fixture_replays_auth_and_all_sixteen_reads(tmp_settings: Path, monkeypatch: Any) -> None:
    data = json.loads((ROOT / "tests/fixtures/native_media_retained_boundaries_v1.json").read_bytes())
    assert data["schemaVersion"] == 1 and data["actualOriginalLoopbackRequests"] == 18
    assert data["actualAuthenticatedRequests"] == 16
    assert len(data["variants"]) == 3
    for name, digest in data["originalInputHashes"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    assert library.store_path().resolve().is_relative_to(tmp_settings.resolve())
    library.media_dir().mkdir(parents=True, exist_ok=True)
    probe = load_wire_probe(monkeypatch)
    requests = 0
    with _running_server() as server:
        for case in data["variants"]:
            raw = case["document"].encode("utf8")
            assert hashlib.sha256(raw).hexdigest() == case["documentSha256"]
            library.store_path().write_bytes(raw)
            if requests == 0:
                for key in ("unauthorized", "workspaceHomeUnauthorized"):
                    denial = data[key]
                    status, body, _ = _request(server, "GET", denial["uri"])
                    assert status == denial["status"] == 401
                    assert probe.exact_json_values_equal(probe.load_exact_json(body), probe.load_exact_json(denial["bodyJson"]))
                    assert library.store_path().read_bytes() == raw
                    requests += 1
            for role, expected in case["results"].items():
                status, body, response = _request(server, "GET", expected["uri"], headers=_auth_headers())
                assert status == expected["status"], (case["name"], role, status)
                assert response.getheader("Content-Type", "").startswith("application/json")
                assert hashlib.sha256(expected["bodyJson"].encode("utf8")).hexdigest() == expected["bodySha256"]
                actual = probe.load_exact_json(body)
                if expected.get("bodyComparisonScope"):
                    assert expected["bodyComparisonScope"] == ["ok", "recent.media", "counts.media"]
                    actual = {"ok": actual["ok"], "media": actual["recent"]["media"], "count": actual["counts"]["media"]}
                assert probe.exact_json_values_equal(actual, probe.load_exact_json(expected["bodyJson"])), (case["name"], role)
                assert library.store_path().read_bytes() == raw
                requests += 1
    assert requests == 18


def test_media_probe_rejects_redirects_head_bytes_and_rounded_numbers(monkeypatch: Any) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    probe = load_wire_probe(monkeypatch)
    seen: list[str] = []

    class WireHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            seen.append(self.path)
            raw = b'{"number":1e-4000}'
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/number")
                raw = b""
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def do_HEAD(self) -> None:
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.path == "/head-leak":
                self.wfile.write(b"leak")

    with HTTPServer(("127.0.0.1", 0), WireHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            with pytest.raises(probe.ProbeFailure, match="NATIVE_MEDIA_UNEXPECTED_REDIRECT"):
                probe.request(origin, "/redirect", "GET", None)
            assert seen == ["/redirect"]
            with pytest.raises(probe.ProbeFailure, match="NATIVE_MEDIA_HEAD_RETURNED_BODY"):
                probe.request(origin, "/head-leak", "HEAD", None)
            assert probe.request(origin, "/head-ok", "HEAD", None) == (200, None)
            status, result = probe.request(origin, "/number", "GET", None, exact_numbers=True)
            assert status == 200
            zero = probe.load_exact_json(b'{"number":0.0}')
            assert not probe.exact_json_values_equal(result, zero)
            assert not probe.exact_json_values_equal(zero, probe.load_exact_json(b'{"number":-0.0}'))
            assert not probe.exact_json_values_equal(zero, probe.load_exact_json(b'{"number":0}'))
            cases, _ = probe.frozen_retained_cases()
            assert len(cases) == 37 and sum(len(case["reads"]) for case in cases) == 152
        finally:
            server.shutdown()
            thread.join(timeout=5)
