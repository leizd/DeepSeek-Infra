"""Index media segments into Local RAG."""

from __future__ import annotations

import time
from typing import Any

from deepseek_infra.infra.media import citations
from deepseek_infra.infra.rag import local_rag

COLLECTION_MEDIA = "media"


def media_item_id(media_id: str, segment_id: str, chunk_index: int = 0) -> str:
    return f"media:{media_id}:{segment_id}:{int(chunk_index)}"


def index_media_segments(media: dict[str, Any], segments: list[dict[str, Any]]) -> int:
    items: list[dict[str, Any]] = []
    media_id = str(media.get("mediaId") or "")
    project_id = str(media.get("projectId") or "")
    for index, segment in enumerate(segments):
        text = str(segment.get("text") or "").strip()
        segment_id = str(segment.get("segmentId") or "")
        if not text or not segment_id:
            continue
        raw_citation = segment.get("citation")
        citation: dict[str, Any] = raw_citation if isinstance(raw_citation, dict) else citations.citation_for_segment(media, segment, ordinal=index + 1)
        metadata: dict[str, Any] = {
            "sourceType": "media",
            "mediaId": media_id,
            "segmentId": segment_id,
            "segmentType": str(segment.get("type") or ""),
            "page": int(segment.get("page") or 0),
            "timeRange": segment.get("timeRange") if isinstance(segment.get("timeRange"), list) else [],
            "framePath": str(segment.get("framePath") or ""),
            "citation": str(citation.get("uri") or ""),
            "citationLabel": str(citation.get("label") or ""),
            "hash": local_rag.chunk_hash(text),
        }
        items.append(
            {
                "item_id": media_item_id(media_id, segment_id, index),
                "collection": COLLECTION_MEDIA,
                "source_id": media_id,
                "project_id": project_id,
                "chunk_index": index,
                "name": str(media.get("title") or media_id),
                "kind": str(media.get("type") or "media"),
                "scope": "",
                "text": text,
                "embedding": local_rag.embed_text(text),
                "metadata": metadata,
                "updated_at": int(time.time() * 1000),
            }
        )
    local_rag.delete_items(collection=COLLECTION_MEDIA, source_id=media_id, project_id=project_id)
    return local_rag.upsert_items(items)


def delete_media_index(media_id: str, *, project_id: str = "") -> int:
    return local_rag.delete_items(collection=COLLECTION_MEDIA, source_id=media_id, project_id=project_id)


def search_media_index(query: str, *, project_id: str | None = None, media_id: str = "", limit: int = 8) -> list[local_rag.RAGSearchResult]:
    return local_rag.search(query, collection=COLLECTION_MEDIA, source_id=media_id, project_id=project_id, limit=limit)
