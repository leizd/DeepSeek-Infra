from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from deepseek_infra.core.config import settings
from deepseek_infra.infra.rag import local_rag
from deepseek_infra.web.server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_GATEWAY_SRC = REPO_ROOT / "rust/crates/deepseek-gateway/src"
RUST_GATEWAY_LIB = RUST_GATEWAY_SRC / "lib.rs"
INVENTORY_SNAPSHOT = REPO_ROOT / "release/production_runtime_inventory_v1.json"


def _auth_headers() -> dict[str, str]:
    if settings.auth.enabled and settings.auth.token:
        return {"Authorization": f"Bearer {settings.auth.token}"}
    return {}


def test_rag_action_routes_registered_and_removed_from_inventory() -> None:
    lib_src = RUST_GATEWAY_LIB.read_text(encoding="utf-8")
    rag_src = (RUST_GATEWAY_SRC / "rag_action_routes.rs").read_text(encoding="utf-8")
    inventory = json.loads(INVENTORY_SNAPSHOT.read_text(encoding="utf-8"))

    assert ".merge(rag_action_routes::router())" in lib_src
    assert '"/api/rag/reindex"' in rag_src
    assert '"/api/rag/verify-citation"' in rag_src
    assert '"/api/rag/eval"' in rag_src

    python_only = {
        (item["method"], item["path"])
        for item in inventory.get("http_routes_only_on_python", [])
    }
    assert ("post", "/api/rag/reindex") not in python_only
    assert ("post", "/api/rag/verify-citation") not in python_only
    assert ("post", "/api/rag/eval") not in python_only


def test_rag_verify_citation_and_eval_python_oracle_parity(tmp_settings: Path) -> None:
    local_rag.upsert_items(
        [
            {
                "item_id": "chunk-p1",
                "collection": local_rag.COLLECTION_FILES,
                "source_id": "doc-fastcdc",
                "project_id": "p1",
                "chunk_index": 0,
                "name": "arch.md",
                "kind": "file",
                "scope": "project:p1",
                "text": "FastCDC deduplication reduces cloud egress across incremental snapshots",
                "metadata": {"lineStart": 10, "lineEnd": 14, "page": 2, "hash": "abc"},
            }
        ]
    )

    client = TestClient(create_app(), base_url="http://127.0.0.1")
    headers = _auth_headers()

    cit = client.post(
        "/api/rag/verify-citation",
        headers=headers,
        json={"itemId": "chunk-p1", "snippet": "FastCDC deduplication"},
    )
    assert cit.status_code == 200
    body = cit.json()
    assert body["ok"] is True
    assert body["citation"]["grounded"] is True
    assert body["citation"]["coverage"] == 1.0
    assert body["citation"]["lineage"]["citation"] == "doc-fastcdc:L10-L14"

    ev = client.post(
        "/api/rag/eval",
        headers=headers,
        json={"cases": [{"query": "FastCDC deduplication", "relevant": ["doc-fastcdc"]}], "k": 3},
    )
    assert ev.status_code == 200
    ev_body = ev.json()
    assert ev_body["ok"] is True
    assert ev_body["eval"]["recallAtK"] == 1.0
    assert ev_body["eval"]["mrr"] == 1.0

    re = client.post(
        "/api/rag/reindex",
        headers=headers,
        json={"action": "rebuild"},
    )
    assert re.status_code == 200
    re_body = re.json()
    assert re_body["ok"] is True
    assert re_body["files"] == 0
    assert re_body["chunks"] == 0
    assert re_body["localRag"]["enabled"] is True
    assert re_body["localRag"]["indexedItems"] == 0
