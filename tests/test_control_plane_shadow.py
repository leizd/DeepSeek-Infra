from __future__ import annotations

from pathlib import Path

from scripts.control_plane_shadow import check_fixture, evaluate


ROOT = Path(__file__).resolve().parents[1]


def test_python_shadow_oracle_matches_frozen_digests() -> None:
    report = check_fixture()
    assert report["ok"] is True
    assert report["passed"] == report["total"]
    assert report["total"] >= 8


def test_shadow_kernel_never_authorizes_mutation() -> None:
    decision = evaluate({"actions": [], "capacityTargets": [], "federationTransitions": []})
    assert decision["mutationDenied"] is True
    assert decision["digest"]


def test_stale_epoch_is_rejected_before_admit() -> None:
    decision = evaluate(
        {
            "nowUnix": 10,
            "nowMinute": 0,
            "liveEpochs": {"act-1": 4},
            "actions": [{"actionId": "act-1", "executionEpoch": 3, "type": "CREATE_REPAIR_JOB", "severity": "warning", "createdAtUnix": 10}],
            "capacityTargets": [],
            "federationTransitions": [],
        }
    )
    assert decision["scheduler"]["admissions"][0]["reason"] == "STALE_EXECUTION_EPOCH"
    assert decision["scheduler"]["orderedActionIds"] == []


def test_pending_to_active_is_not_tofu() -> None:
    decision = evaluate(
        {
            "localFleetId": "fleet-a",
            "federationTransitions": [
                {
                    "peerFleetId": "fleet-b",
                    "from": "PENDING",
                    "to": "ACTIVE",
                    "metadata": {"provider": "minio", "region": "us", "jurisdiction": "us", "siteClass": "region"},
                }
            ],
        }
    )
    assert decision["federation"]["transitions"][0]["code"] == "FEDERATION_PEER_NOT_VERIFIED"


def test_dual_track_verifier_detects_match_and_mismatch(tmp_path: Path) -> None:
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from typing import Any
    from scripts.control_plane_shadow import ShadowError, evaluate, verify_against_go

    dummy_fixture = tmp_path / "cases.json"
    dummy_fixture.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "case-1",
                        "snapshot": {"nowUnix": 10, "nowMinute": 0, "actions": [], "capacityTargets": [], "federationTransitions": []},
                        "expect": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    py_decision = evaluate({"nowUnix": 10, "nowMinute": 0, "actions": [], "capacityTargets": [], "federationTransitions": []})
    expected_digest = py_decision["digest"]

    class MockGoHandler(BaseHTTPRequestHandler):
        digest_to_return = expected_digest

        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"digest": self.digest_to_return}).encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), MockGoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        res = verify_against_go(f"http://127.0.0.1:{port}", dummy_fixture)
        assert res["ok"] is True
        assert res["dual_track_verified"] == 1

        MockGoHandler.digest_to_return = "tampered_digest_123"
        try:
            verify_against_go(f"http://127.0.0.1:{port}", dummy_fixture)
            assert False, "should fail on mismatch"
        except ShadowError as exc:
            assert "Dual-track parity failure" in str(exc)
    finally:
        server.shutdown()
