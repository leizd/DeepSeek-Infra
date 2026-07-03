#!/usr/bin/env python3
"""Offline media retrieval and citation eval for v2.7.0."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from scripts.smoke_media import configure_runtime_root  # noqa: E402


def build_report(version: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="deepseek-media-eval-") as tmp:
        root = Path(tmp)
        configure_runtime_root(root)
        from deepseek_infra.infra.media import evidence, ingestion, library
        from deepseek_infra.infra.rag import local_rag
        from deepseek_infra.infra.workspace import projects

        project = projects.create_project("Media Eval")
        project_id = str(project["projectId"])
        pdf = ingestion.register_from_payload(
            {
                "projectId": project_id,
                "type": "pdf",
                "title": "Eval PDF",
                "mimeType": "application/pdf",
                "pageTexts": [{"page": 1, "text": "First-class media objects are indexed into Local RAG."}],
                "process": True,
            }
        )
        webpage = ingestion.register_from_payload(
            {
                "projectId": project_id,
                "type": "webpage",
                "title": "Eval Webpage",
                "html": "<main><p>Webpage text is citable media evidence.</p></main>",
                "process": True,
            }
        )
        cases = [
            {"query": "first-class media objects", "relevant": [pdf["mediaId"]]},
            {"query": "webpage citable media", "relevant": [webpage["mediaId"]]},
        ]
        recall = local_rag.evaluate_recall(cases, k=3, collection=local_rag.COLLECTION_MEDIA)
        all_segments = [*library.list_segments(str(pdf["mediaId"])), *library.list_segments(str(webpage["mediaId"]))]
        citation_pass = all(isinstance(segment.get("citation"), dict) and str(segment["citation"].get("uri") or "").startswith("media://") for segment in all_segments)
        checks = {
            "mediaRecall": "PASS" if recall["recallAtK"] >= 1.0 else "FAIL",
            "mediaCitations": "PASS" if citation_pass else "FAIL",
            "mediaRagCollection": "PASS" if local_rag.status().get("indexedMedia", 0) >= 2 else "FAIL",
        }
        status = "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL"
        report = evidence.evidence_metadata(version, status=status, checks=checks, details={"recall": recall, "projectId": project_id})
        report["summary"] = {"caseCount": len(cases), "recallAtK": recall["recallAtK"], "mrr": recall["mrr"]}
        return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline media eval")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports" / f"media-v{APP_VERSION}.json"))
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args.version)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": report["checks"], "out": str(target)}, ensure_ascii=False, indent=2))
    return 1 if args.strict and report["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
