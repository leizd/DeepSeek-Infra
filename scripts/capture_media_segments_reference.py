"""Offline original-only processor corpus. Never imported by a production entry point."""
from itertools import count
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.media import citations, indexer, ingestion, processors, schema  # noqa: E402
from deepseek_infra.infra.rag import local_rag  # noqa: E402


def build_reference():
    cases = []

    def add(name, kind, metadata=None, source=b"", **extra):
        media = dict(mediaId="media_0123456789abcdef", projectId="segment-reference",
                     title="Original media", type=kind, metadata=metadata or {}, **extra)
        ids = count(1)
        row = dict(name=name, media=media, sourceHex=source.hex(), ocrEnabled=False)
        with patch.object(processors, "_source_bytes", return_value=source), patch.object(
            schema, "new_segment_id", side_effect=lambda: f"seg_{next(ids):016x}"
        ):
            try:
                raw = processors.extract_segments(media, ocr_enabled=False)
                segments = []
                for i, item in enumerate(raw):
                    segment = schema.normalize_segment(item, media_id=media["mediaId"], fallback_index=i)
                    segment["citation"] = citations.citation_for_segment(media, segment, ordinal=i + 1)
                    segments.append(schema.normalize_segment(segment, media_id=media["mediaId"], fallback_index=i))
                row["expected"] = dict(segments=segments, metadata=ingestion._segment_metadata(segments, indexed=sum(bool(s["text"].strip()) for s in segments)))
                index_rows = []
                with patch.object(local_rag, "delete_items", return_value=0), patch.object(
                    local_rag, "upsert_items", side_effect=lambda items: index_rows.extend(items) or len(items)
                ), patch.object(local_rag, "embed_text", side_effect=lambda text: local_rag.hash_text_embedding(text, dimensions=64)), patch.object(indexer.time, "time", return_value=1_700_000_000.123):
                    indexer.index_media_segments(media, segments)
                for item in index_rows:
                    item["vector_id"] = local_rag.stable_vector_id(item["item_id"])
                    item["embedding"] = local_rag.normalize_vector(item["embedding"], 64)
                    item["text"] = item["text"][:local_rag.MAX_RAG_TEXT_CHARS]
                row["expected"]["indexItems"] = index_rows
            except AppError as error:
                row["error"] = dict(code=error.code.value, message=str(error), status=error.status)
        cases.append(row)

    add("webpage-html", "webpage", source=b"<h1>neptune</h1><script>secret()</script><p>Original &amp; source</p>")
    add("webpage-explicit-text", "webpage", {"webpageText": "  neptune \u4e2d\u6587  "}, source=b"<p>ignored</p>")
    add("webpage-empty", "webpage")
    add("webpage-scalar", "webpage", {"text": {"nested": 7}})
    add("image-ocr-caption", "image", {"ocrText": "neptune OCR", "caption": "Original caption", "ocrConfidence": "0.876543"})
    add("image-default-caption", "image")
    add("image-duplicate-caption", "image", {"text": "same", "caption": "same"})
    add("image-confidence-clamp", "image", {"text": "text", "ocrConfidence": -3})
    add("image-confidence-invalid", "image", {"text": "text", "ocrConfidence": "unusable"})
    add("screenshot-text", "screenshot", {"text": "neptune screenshot"})
    add("pdf-imported-pages", "pdf", {"pageTexts": [{"page": 3, "text": " neptune third "}, " second ", {"page": 0, "text": ""}, {"page": -1, "text": "negative page"}]})
    add("pdf-json-pages", "pdf", {"pages": '[{"page":2,"text":"neptune"},"second"]'})
    add("pdf-invalid-pages-json", "pdf", {"pageTexts": "not json", "text": "fallback", "pageCount": 3})
    add("pdf-fallback-pages", "pdf", {"pdfText": "abcdefghijklmnop", "pageCount": 3})
    add("pdf-empty", "pdf")
    add("pdf-invalid-bytes", "pdf", source=b"invalid original PDF")
    add("audio-empty", "audio")
    add("audio-text-duration", "audio", {"transcript": "neptune\nsecond line", "durationSec": 12})
    add("audio-multichunk", "audio", {"transcript": "neptune \u4e2d\u6587 " * 380, "durationSec": 24})
    add("audio-long-word", "audio", {"transcript": "\u4e2d" * 1201 + " other", "durationSec": 2})
    add("audio-whitespace", "audio", {"transcript": "\r\n one \r two \u2028three\r\nfour \t "})
    add("audio-imported-sorted", "audio", {"transcriptSegments": [{"text": "late", "timeRange": [10.12345, 8], "confidence": 2}, {"text": "early", "startSec": -2, "endSec": "3.4567"}, "no time", {"transcript": "tie", "timeRange": [0, 1]}, {"text": ""}]})
    add("audio-imported-empty-fallback", "audio", {"segments": ["", {}], "transcriptText": "fallback", "durationSec": "bad"})
    add("video-frames-transcript", "video", {"transcript": "neptune spoken", "durationSec": 12, "frames": [{"caption": "late", "timeRange": [8, 9], "framePath": "objects/frame.png"}, {"text": "early", "startSec": 2, "endSec": 3}, "untimed"]})
    add("video-imported-transcript", "video", {"transcriptSegments": [{"text": "neptune", "timeRange": [60, 62]}], "frameCaptions": ["frame"]})
    add("video-no-transcript", "video", {"frames": [{"text": "", "timeRange": [1, 2]}, "frame"]})
    add("video-frame-path-refused", "video", {"frames": [{"text": "frame", "framePath": "../escape"}]})
    add("unsupported-media-type", "other")
    doc = dict(schemaVersion=1, role="OFFLINE_ORIGINAL_PROCESSOR_REFERENCE", cases=cases,
               pythonProductionRequired=False, nativeRuntimeQualified=False)
    return doc


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write",action="store_true",help="Explicitly regenerate the offline reference fixture")
    args=parser.parse_args()
    path=ROOT/"tests/native/media_segments_reference_v1.json"
    data=(json.dumps(build_reference(),ensure_ascii=True,indent=2,allow_nan=False)+"\n").encode()
    if args.write:
        path.write_bytes(data)
    else:
        assert path.read_bytes()==data,"Original media processor reference drifted"
    print(json.dumps(dict(status="PASS",scope="Offline original media processor reference only",cases=28,nativeRuntimeQualified=False)))


if __name__=="__main__":
    main()
