"""Evidence helpers for Automation Runtime smoke and eval reports."""

from __future__ import annotations

from typing import Any

from deepseek_infra.infra.skills.evidence import release_evidence_payload


def automation_evidence_payload(version: str, *, checks: dict[str, str], details: dict[str, Any] | None = None) -> dict[str, Any]:
    return release_evidence_payload(version=version, checks=checks, details=details or {})
