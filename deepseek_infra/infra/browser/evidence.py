"""Evidence metadata helpers for Browser runtime smoke and eval reports."""

from __future__ import annotations

from typing import Any

from deepseek_infra.infra.media.evidence import evidence_metadata


def browser_evidence_payload(version: str, *, checks: dict[str, str], details: dict[str, Any] | None = None) -> dict[str, Any]:
    status = "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL"
    return evidence_metadata(version, status=status, checks=checks, details=details or {})
