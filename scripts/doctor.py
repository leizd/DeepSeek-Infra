#!/usr/bin/env python3
"""Runtime Doctor — environment & runtime readiness checks.

Run offline (CI-safe, no network, no API key required):

    python scripts/doctor.py --offline

Run against a live server (also probes /healthz /readyz /metrics):

    python scripts/doctor.py --base-url http://127.0.0.1:8000

Exits 1 only on FAIL; WARNINGs do not fail the run. Use --json for a
machine-readable summary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import settings  # noqa: E402
from deepseek_infra.infra.diagnostics import runtime_doctor as doctor  # noqa: E402


def build_options(args: argparse.Namespace) -> doctor.DoctorOptions:
    root = args.root.resolve() if args.root else settings.root
    static_dir = settings.static_dir
    return doctor.DoctorOptions(
        root=root,
        static_dir=static_dir,
        offline=bool(args.offline) and not bool(args.with_server),
        base_url=args.base_url.rstrip("/"),
        token=args.token,
        host=args.host,
        port=args.port,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeepSeek Infra runtime readiness doctor")
    parser.add_argument("--offline", action="store_true", help="Skip network probes; do not require an API key or a running server.")
    parser.add_argument("--with-server", action="store_true", help="Probe /healthz /readyz /metrics against --base-url.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL for live health probes.")
    parser.add_argument("--token", default="", help="Local auth token (unused by the unauth health probes; accepted for symmetry).")
    parser.add_argument("--host", default=settings.default_host, help="Host for the port-availability check.")
    parser.add_argument("--port", type=int, default=settings.default_port, help="Port for the port-availability check.")
    parser.add_argument("--root", type=Path, default=None, help="Data root to check. Defaults to the configured runtime root.")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable JSON summary.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options = build_options(args)
    results = doctor.run_doctor(options)
    if args.json:
        print(doctor.dump_json(results))
    else:
        print(doctor.render_text(results))
    return doctor.exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
