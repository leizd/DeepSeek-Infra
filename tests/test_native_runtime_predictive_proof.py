from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import resilience_predictive_proof
from scripts.native_runtime_contract import validate_corpus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "compat/native-runtime/v8/evidence/predictive_planning_proof_vector.json"


def _rebind_proof(proof: dict[str, Any]) -> None:
    body = {key: value for key, value in proof.items() if key != "proofDigest"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    proof["proofDigest"] = hashlib.sha256(canonical).hexdigest()


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


def test_v8_predictive_corpus_replays_through_python_oracle() -> None:
    fixture = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1
    assert fixture["source_version"] == "4.8.0"
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert resilience_predictive_proof.validate_predictive_planning_proof(fixture["valid_proof"]) == []

    for mutation in fixture["invalid_mutations"]:
        proof = copy.deepcopy(fixture["valid_proof"])
        _replace_pointer(proof, mutation["pointer"], mutation["value"])
        if mutation["rebind_proof"]:
            _rebind_proof(proof)
        assert resilience_predictive_proof.validate_predictive_planning_proof(proof) == mutation["expected_errors"], mutation["name"]


def test_v8_predictive_corpus_digest_is_pinned() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v8/manifest.json")
    item = next(entry for entry in manifest["corpora"] if entry["id"] == "predictive-planning-proof-semantics-v8")
    assert item["sha256"] == "ae28ddef148eea156f4b4a1d98ff07bbcf59e1963ac299b4c018efbe608d9ece"
