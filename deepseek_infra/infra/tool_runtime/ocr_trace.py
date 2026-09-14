"""Request-scoped OCR correlation identifiers and stage timings.

The Android OCR path had neither a correlation identifier nor stage-level timing,
while the optional Rust sidecar has had both since 3.8.0 (``rust_core.transport``
correlation ids plus the ``X-DeepSeek-Rust-Processing-Us`` response header).  This
module ports that discipline to OCR without adding a transport, an endpoint, or any
wire contract:

* one log-safe, system-generated correlation identifier per OCR request;
* a fixed set of Python-owned stage timers (``select`` / ``transport`` /
  ``normalize`` / ``score``);
* an optional backend breakdown reported by the Android JNI bridge
  (``decode`` / ``render`` / ``recognize`` / ``overhead``).

Byte marshalling across the Chaquopy/JNI boundary is *not* separately measurable
from Python: it happens inside the single ``recognizeImage`` / ``recognizePdf``
call, so it stays inside ``transport``.  The bridge's own breakdown is what
separates image decode, PDF page rendering, and ML Kit inference from each other.

Only engine names, stage durations, and counts are recorded.  Recognized text,
file paths, and credentials are never copied into the trace, matching the sidecar
rule that diagnostics carry no content.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

OCR_STAGE_SELECT = "select"
OCR_STAGE_TRANSPORT = "transport"
OCR_STAGE_NORMALIZE = "normalize"
OCR_STAGE_SCORE = "score"
OCR_STAGES: tuple[str, ...] = (
    OCR_STAGE_SELECT,
    OCR_STAGE_TRANSPORT,
    OCR_STAGE_NORMALIZE,
    OCR_STAGE_SCORE,
)

OCR_BACKEND_STAGES: tuple[str, ...] = ("decode", "render", "recognize", "overhead")

MAX_CORRELATION_ID_CHARS = 64

# Bounded, log-safe alphabet: no whitespace, quotes, or control characters can
# reach a log line through a caller-supplied identifier.
_CORRELATION_ALPHABET = frozenset("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_.:")


def new_ocr_correlation_id() -> str:
    """Return a log-safe, system-generated OCR correlation identifier."""
    return uuid.uuid4().hex


def sanitize_correlation_id(value: object) -> str:
    """Accept a caller-supplied identifier only when it is log-safe and bounded.

    An empty string means "not usable"; callers then generate a fresh identifier.
    """
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > MAX_CORRELATION_ID_CHARS:
        return ""
    if any(char not in _CORRELATION_ALPHABET for char in candidate):
        return ""
    return candidate


def derive_ocr_correlation_id(base: str, ordinal: int) -> str:
    """Derive a per-item identifier from one request identifier.

    A multi-file upload produces one media object per file, so each needs its own
    trace; sharing one object would accumulate the files' stage timings together.
    The composite is kept inside ``MAX_CORRELATION_ID_CHARS`` so it is never
    rejected by ``sanitize_correlation_id`` and silently replaced.
    """
    safe_base = sanitize_correlation_id(base)
    if not safe_base:
        return new_ocr_correlation_id()
    suffix = f".{max(1, int(ordinal))}"
    return f"{safe_base[: MAX_CORRELATION_ID_CHARS - len(suffix)]}{suffix}"


@dataclass
class OcrTrace:
    """Correlation identifier plus stage timings for one OCR request.

    ``trace=None`` everywhere in the OCR call chain means "no telemetry": the
    recognizer then behaves exactly as before and pays no dictionary cost.
    """

    correlation_id: str = ""
    mode: str = ""
    engine: str = ""
    input_bytes: int = 0
    pages: int = 0
    stages_us: dict[str, int] = field(default_factory=dict)
    backend_stages_us: dict[str, int] = field(default_factory=dict)
    attempts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.correlation_id = sanitize_correlation_id(self.correlation_id) or new_ocr_correlation_id()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time one stage, accumulating into the matching bucket on exit."""
        started_ns = time.perf_counter_ns()
        try:
            yield
        finally:
            self.add_stage(name, max(0, (time.perf_counter_ns() - started_ns) // 1000))

    def add_stage(self, name: str, duration_us: int) -> None:
        self.stages_us[name] = self.stages_us.get(name, 0) + max(0, int(duration_us))

    def total_stage_us(self) -> int:
        return sum(self.stages_us.get(name, 0) for name in OCR_STAGES)

    def recorded(self) -> bool:
        """True when this trace observed OCR work worth persisting.

        Media that never reached an OCR engine (webpage, audio, video, or a PDF
        with selectable text) records nothing, so its metadata shape is unchanged.
        """
        return bool(self.attempts or self.stages_us)

    def note_attempt(self, engine: str, outcome: str) -> None:
        """Record a low-cardinality engine outcome. Never the recognized text."""
        entry = f"{engine}:{outcome}"
        if entry not in self.attempts:
            self.attempts.append(entry)

    def record_backend(self, value: Any) -> bool:
        """Adopt a stage breakdown reported by the Android JNI bridge.

        Returns ``True`` when at least one stage was accepted.  Unknown, missing,
        or malformed keys are ignored so a bridge that reports nothing (or a newer
        bridge reporting more) can never fail the OCR call.
        """
        if not isinstance(value, dict):
            return False
        recorded = False
        for name in OCR_BACKEND_STAGES:
            raw = value.get(f"{name}Us")
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                self.backend_stages_us[name] = self.backend_stages_us.get(name, 0) + max(0, int(raw))
                recorded = True
        raw_pages = value.get("pages")
        if isinstance(raw_pages, int) and not isinstance(raw_pages, bool) and raw_pages > 0:
            self.pages = max(self.pages, raw_pages)
        return recorded

    def details(self) -> dict[str, Any]:
        """Compact, content-free payload for an ``AppError.details`` field."""
        payload: dict[str, Any] = {"correlationId": self.correlation_id}
        if self.engine:
            payload["engine"] = self.engine
        if self.attempts:
            payload["attempts"] = list(self.attempts)
        return payload

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "correlationId": self.correlation_id,
            "mode": self.mode,
            "engine": self.engine,
            "inputBytes": int(self.input_bytes),
            "pages": int(self.pages),
            "timingsUs": {name: int(self.stages_us.get(name, 0)) for name in OCR_STAGES},
            "totalUs": int(self.total_stage_us()),
            "attempts": list(self.attempts),
        }
        if self.backend_stages_us:
            payload["backendTimingsUs"] = {
                name: int(self.backend_stages_us.get(name, 0)) for name in OCR_BACKEND_STAGES
            }
        return payload


@contextmanager
def ocr_stage(trace: OcrTrace | None, name: str) -> Iterator[None]:
    """Time ``name`` on ``trace``; a no-op when no telemetry was requested."""
    if trace is None:
        yield
        return
    with trace.stage(name):
        yield


def note_attempt(trace: OcrTrace | None, engine: str, outcome: str) -> None:
    """Record a low-cardinality engine outcome when telemetry is requested."""
    if trace is not None:
        trace.note_attempt(engine, outcome)


def record_backend_timings(trace: OcrTrace | None, engine: object) -> bool:
    """Adopt an engine's bridge-reported stage breakdown, if it exposes one."""
    if trace is None:
        return False
    return trace.record_backend(getattr(engine, "last_backend_timings", None))
