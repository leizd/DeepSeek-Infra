#!/usr/bin/env python3
"""Offline Edge Router smoke for route-preview, status and routing policy."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402


def git_short_sha() -> str:
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def app_version() -> str:
    from deepseek_infra.core.config import settings

    return settings.app_version


def fake_options() -> Any:
    from deepseek_infra.infra.gateway.edge_inference import EdgeOptions

    return EdgeOptions(
        enabled=True,
        provider="fake",
        model_path="",
        model_name="edge-fake",
        chat_format="",
        n_ctx=4096,
        n_threads=0,
        n_gpu_layers=0,
        max_tokens=128,
        temperature=0.0,
        top_p=1.0,
        simple_max_chars=6000,
    )


def route_preview(payload: dict[str, Any], *, cloud_available: bool = True, available: bool = True) -> dict[str, Any]:
    from deepseek_infra.infra.gateway.edge_inference import decide_edge_route, edge_manager, edge_route_decision_payload

    options = fake_options()
    status = edge_manager.status(options)
    if not available:
        status = {**status, "available": False, "dependencyAvailable": False, "suggestions": ["Install fake edge provider."]}
    return edge_route_decision_payload(decide_edge_route(payload, options=options, status=status, cloud_available=cloud_available))


def route_paths() -> set[str]:
    from deepseek_infra.web.server import create_app

    def collect(routes: list[Any]) -> set[str]:
        paths: set[str] = set()
        for route in routes:
            path = getattr(route, "path", "")
            if path:
                paths.add(path)
            original = getattr(route, "original_router", None)
            if original is not None:
                paths |= collect(getattr(original, "routes", []))
        return paths

    return collect(create_app().routes)



def run_edge_smoke() -> tuple[dict[str, str], dict[str, Any]]:
    from deepseek_infra.core.errors import AppError
    from deepseek_infra.infra.diagnostics.runtime_doctor import check_edge_router
    from deepseek_infra.infra.gateway.edge_inference import fake_completion

    checks: dict[str, str] = {
        "edgeDoctor": "FAIL",
        "statusShape": "FAIL",
        "routePreviewApi": "FAIL",
        "fakeProvider": "FAIL",
        "routingPolicy": "FAIL",
        "fallbackPolicy": "FAIL",
        "forcedLocalUnavailable": "FAIL",
    }
    details: dict[str, Any] = {}

    doctor_result = check_edge_router()
    checks["edgeDoctor"] = "PASS" if doctor_result.status in {"pass", "warn"} and doctor_result.data.get("provider") else "FAIL"
    details["edgeDoctor"] = doctor_result.to_dict()

    simple_payload = {"edgeMode": "auto", "messages": [{"role": "user", "content": "Summarize this in one sentence."}]}
    current_payload = {"edgeMode": "auto", "messages": [{"role": "user", "content": "What is the latest AI news today?"}]}
    image_payload = {
        "edgeMode": "auto",
        "messages": [
            {
                "role": "user",
                "content": "Describe this image.",
                "attachments": [{"imageData": "data:image/png;base64,AAAA"}],
            }
        ],
    }

    simple = route_preview(simple_payload)
    current = route_preview(current_payload)
    image = route_preview(image_payload)
    fallback = route_preview(simple_payload, cloud_available=False)
    details["previews"] = {"simple": simple, "current": current, "image": image, "fallback": fallback}

    checks["statusShape"] = "PASS" if simple["status"].get("providerSupported") and simple["status"].get("available") else "FAIL"
    checks["routePreviewApi"] = "PASS" if "/api/edge/route-preview" in route_paths() else "FAIL"
    completion = fake_completion([{"role": "user", "content": "hello"}], fake_options())
    checks["fakeProvider"] = "PASS" if completion.provider == "fake" and completion.content.startswith("[edge fake]") else "FAIL"
    routing_pass = (
        simple["useEdge"] is True
        and simple["reason"] == "simple_task_local"
        and current["useEdge"] is False
        and current["reason"] == "complex_task_cloud"
        and image["useEdge"] is False
        and image["reason"] == "unsupported_payload"
    )
    checks["routingPolicy"] = "PASS" if routing_pass else "FAIL"
    checks["fallbackPolicy"] = "PASS" if fallback["useEdge"] is True and fallback["reason"] == "cloud_unavailable_simple_local" else "FAIL"
    try:
        route_preview({"edgeMode": "local", "messages": [{"role": "user", "content": "hello"}]}, available=False)
    except AppError as exc:
        checks["forcedLocalUnavailable"] = "PASS" if exc.status == 409 else "FAIL"

    return checks, details


def build_evidence(checks: dict[str, str], details: dict[str, Any]) -> dict[str, Any]:
    status = "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL"
    revision = evidence_revision(REPO_ROOT)
    return {
        "version": app_version(),
        "commit": revision["testedRevision"],
        **revision,
        "generatedAt": utc_now(),
        "environment": {"os": platform.platform(), "python": platform.python_version(), "ci": bool(os.environ.get("CI"))},
        "status": status,
        "checks": checks,
        "details": details,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Edge Router stabilization smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "evidence" / f"edge-router-v{app_version()}.json")
    parser.add_argument("--json", action="store_true", help="Print evidence JSON to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checks, details = run_edge_smoke()
    evidence = build_evidence(checks, details)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
