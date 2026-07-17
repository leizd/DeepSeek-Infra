"""Run the bounded 4.0.3 Rust sidecar release benchmark suite."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.bench_rust_sidecar_release import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
