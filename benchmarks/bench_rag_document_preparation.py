"""Informational offline benchmark for Python and optional Rust document preparation."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.rag.document_preparation import prepare_rag_document  # noqa: E402


def benchmark_profiles() -> list[tuple[str, str, int, int]]:
    return [
        ("small", "short document\n" * 64, 6000, 400),
        ("medium", ("paragraph alpha beta gamma\n\n" * 2000)[:50_000], 6000, 400),
        ("large", ("deterministic document content\n" * 20_000)[:500_000], 6000, 400),
        ("high-overlap", ("overlap measurement line\n" * 5000)[:100_000], 2000, 1500),
        ("cjk-heavy", ("\u4e2d\u6587\u6587\u6863\u5206\u5757\u6d4b\u8bd5\uff0c\u7a33\u5b9a\u5b57\u7b26\u504f\u79fb\u3002\n" * 6000)[:100_000], 6000, 400),
    ]


def _payload(name: str, text: str, chunk_chars: int, chunk_overlap: int) -> dict[str, Any]:
    return {
        "documentId": f"benchmark-{name}",
        "text": text,
        "metadata": {"displayName": f"{name}.txt", "sourceType": "text/plain"},
        "chunking": {"chunkChars": chunk_chars, "chunkOverlap": chunk_overlap},
    }


def _median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples) * 1000, 3)


def _rust_request(base_url: str, raw: bytes, timeout: float) -> dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}/rag/documents/prepare",
        data=raw,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied local sidecar URL
            value = json.loads(response.read())
    except HTTPError as exc:
        value = json.loads(exc.read())
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Rust sidecar benchmark request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Rust sidecar benchmark returned non-object JSON")
    return value


def run_benchmark(*, base_url: str | None, rounds: int, timeout: float) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    for name, text, chunk_chars, chunk_overlap in benchmark_profiles():
        payload = _payload(name, text, chunk_chars, chunk_overlap)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        python_samples: list[float] = []
        serialization_samples: list[float] = []
        rust_samples: list[float] = []
        local: dict[str, Any] = {}
        remote: dict[str, Any] | None = None
        for _ in range(rounds):
            started = time.perf_counter()
            local = prepare_rag_document(payload)
            python_samples.append(time.perf_counter() - started)

            started = time.perf_counter()
            json.loads(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            serialization_samples.append(time.perf_counter() - started)

            if base_url is not None:
                started = time.perf_counter()
                remote = _rust_request(base_url, raw, timeout)
                rust_samples.append(time.perf_counter() - started)
        if local.get("ok") is not True or (remote is not None and remote != local):
            raise RuntimeError(f"benchmark parity failed for profile {name}")
        chunks_value = local.get("chunks")
        chunks: list[Any] = chunks_value if isinstance(chunks_value, list) else []
        profiles.append(
            {
                "profile": name,
                "inputCharacterCount": len(text),
                "requestBytes": len(raw),
                "chunkCount": len(chunks),
                "chunkChars": chunk_chars,
                "chunkOverlap": chunk_overlap,
                "rounds": rounds,
                "pythonPreparationMedianMs": _median_ms(python_samples),
                "rustSidecarMedianMs": _median_ms(rust_samples) if rust_samples else None,
                "serializationMedianMs": _median_ms(serialization_samples),
            }
        )
    return {
        "version": "3.7.0",
        "informationalOnly": True,
        "defaultChanged": False,
        "rustSidecarMeasured": base_url is not None,
        "profiles": profiles,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Optional running Rust sidecar URL")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    try:
        report = run_benchmark(base_url=args.base_url, rounds=args.rounds, timeout=args.timeout)
    except RuntimeError as exc:
        print(f"RAG document preparation benchmark failed: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
