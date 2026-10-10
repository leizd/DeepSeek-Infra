from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import evidence_proof
from scripts.native_runtime_contract import validate_corpus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "compat/native-runtime/v9/evidence/autonomous_storage_bytes_vector.json"


def _replace_pointer(document: Any, pointer: str, value: Any) -> None:
    parts = pointer.removeprefix("/").split("/")
    current = document
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    final = parts[-1]
    if isinstance(current, list):
        current[int(final)] = value
    else:
        current[final] = value


def test_v9_storage_evidence_corpus_replays_through_python_oracle() -> None:
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1
    assert fixture["source_version"] == "4.8.0"
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert evidence_proof.validate_autonomous_storage_bytes_proof(fixture["valid_evidence"], "proof") == []
    assert evidence_proof.validate_autonomous_storage_bytes_proof([], "proof") == fixture["non_object_errors"]  # type: ignore[arg-type]

    for mutation in fixture["mutation_cases"]:
        item = copy.deepcopy(fixture["valid_evidence"])
        _replace_pointer(item, mutation["pointer"], mutation["value"])
        assert evidence_proof.validate_autonomous_storage_bytes_proof(item, "proof") == mutation["expected_errors"], mutation["name"]


def test_v9_storage_evidence_corpus_digest_is_pinned() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v9/manifest.json")
    item = next(entry for entry in manifest["corpora"] if entry["id"] == "autonomous-storage-bytes-semantics-v9")
    assert item["sha256"] == "766dcc55feef7a5a3fe22556fa6c2ec566c1e7c45b58c422df81b16ef68feafa"
