"""Generate the checked-in deterministic RAG document preparation corpus."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "fixtures" / "rag" / "document_preparation_cases.json"


def payload(
    text: Any,
    *,
    document_id: Any = "doc-base",
    metadata: Any = None,
    chunk_chars: Any = 12,
    chunk_overlap: Any = 2,
) -> dict[str, Any]:
    return {
        "documentId": document_id,
        "text": text,
        "metadata": {"displayName": "fixture.txt", "sourceType": "text/plain", "kind": "text"} if metadata is None else metadata,
        "chunking": {"chunkChars": chunk_chars, "chunkOverlap": chunk_overlap},
    }


def main() -> None:
    cases: list[dict[str, Any]] = []

    def add(name: str, value: Any = None, *, code: str | None = None, generate: str = "", assertions: dict[str, Any] | None = None) -> None:
        case: dict[str, Any] = {"name": name, "expect": {"ok": code is None}}
        if code is not None:
            case["expect"]["code"] = code
        if generate:
            case["generate"] = generate
        else:
            case["payload"] = value
        if assertions:
            case["assert"] = assertions
        cases.append(case)

    basic_texts = {
        "single_character": "x",
        "short_text": "short text",
        "exact_chunk_size": "abcdefghijkl",
        "chunk_size_plus_one": "abcdefghijklm",
        "two_full_chunks": "abcdefghijklmnopqrstuvwx",
        "last_short_chunk": "abcdefghijklmnop",
        "long_no_newline": "x" * 80,
        "two_paragraphs": "first paragraph\n\nsecond paragraph",
        "many_blank_lines": "alpha\n\n\n\n\nbeta",
        "leading_trailing_space": "  alpha beta  ",
        "tabs": "alpha\tbeta\tgamma",
        "crlf": "alpha\r\nbeta\r\n",
        "cr_only": "alpha\rbeta\r",
        "lf_only": "alpha\nbeta\n",
        "trailing_spaces_per_line": "alpha   \n beta\t \n",
        "nul_removed": "alpha\u0000beta",
        "paragraph_boundary_late": "aaaaaa\n\nbbbbbbb\nccccccc",
        "newline_boundary_late": "abcdefgh\nijklmnop",
        "newline_boundary_early": "a\nbcdefghijklmnop",
        "many_paragraphs": "\n\n".join(f"paragraph {index}" for index in range(20)),
        "numeric_text": "1234567890",
        "punctuation": "alpha, beta; gamma: delta!",
        "markdown": "# Heading\n\n- one\n- two\n",
        "json_text": '{"hello":"world"}',
        "code_text": "def hello():\n    return 'world'\n",
    }
    for name, text in basic_texts.items():
        add(f"basic_{name}", payload(text))

    overlap_cases = [
        ("zero", 8, 0),
        ("one", 8, 1),
        ("two", 8, 2),
        ("half_minus_one", 8, 3),
        ("half", 8, 4),
        ("near_size", 8, 7),
        ("small_zero", 2, 0),
        ("small_one", 2, 1),
        ("three_one", 3, 1),
        ("five_four", 5, 4),
        ("large_small", 64, 2),
        ("large_half", 64, 32),
        ("last_chunk", 9, 3),
        ("multi_chunk", 10, 2),
        ("high_overlap", 20, 19),
    ]
    for name, size, overlap in overlap_cases:
        add(f"overlap_{name}", payload("abcdefghijklmnopqrstuvwxyz" * 3, chunk_chars=size, chunk_overlap=overlap))
    add("overlap_paragraph_boundary_near_size", payload("aaaaaa\nbbbbbbbbbbbb", chunk_chars=10, chunk_overlap=9))

    unicode_texts = {
        "cjk": "中文文档分块测试",
        "mixed_cjk": "DeepSeek 中文 RAG preparation",
        "emoji": "alpha 🚀 beta 🙂 gamma",
        "non_bmp": "A\U00020000B\U0001f9e0C",
        "combining": "Cafe\u0301 and e\u0301cole",
        "fullwidth": "全角，标点。测试！",
        "arabic": "مرحبا بالعالم",
        "japanese": "日本語の文書チャンク",
        "korean": "한국어 문서 청크",
        "multilingual": "English 中文 日本語 한국어 العربية 🚀",
        "unicode_nbsp": "alpha\u00a0beta",
        "unicode_em_space": "alpha\u2003beta",
        "unicode_line_separator": "alpha\u2028beta",
        "unicode_paragraph_separator": "alpha\u2029beta",
        "zero_width_joiner": "developer 👩\u200d💻 notes",
        "variation_selector": "text ✈️ flight",
        "regional_indicators": "flags 🇨🇳🇺🇸",
        "math": "∑ α β γ ∞",
        "devanagari": "हिन्दी दस्तावेज़",
        "thai": "เอกสารภาษาไทย",
    }
    for name, text in unicode_texts.items():
        add(f"unicode_{name}", payload(text, document_id=f"doc-{name}", chunk_chars=6, chunk_overlap=2))

    metadata_values = [
        ("empty", {}),
        ("display_name", {"displayName": "notes.txt"}),
        ("source_type", {"sourceType": "text/plain"}),
        ("kind", {"kind": "text"}),
        ("all", {"displayName": "notes.txt", "sourceType": "text/plain", "kind": "text"}),
        ("numeric", {"displayName": 7, "sourceType": "text/plain"}),
        ("boolean", {"displayName": True, "sourceType": "text/plain"}),
        ("null", {"displayName": None, "sourceType": "text/plain"}),
        ("float", {"displayName": 1.5, "sourceType": "text/plain"}),
        ("unknown_string", {"displayName": "x.txt", "unknown": "ignored"}),
        ("unknown_number", {"displayName": "x.txt", "unknown": 9}),
        ("unknown_object", {"displayName": "x.txt", "unknown": {"nested": True}}),
        ("unknown_array", {"displayName": "x.txt", "unknown": [1, 2, 3]}),
        ("unicode_name", {"displayName": "中文🚀.txt", "sourceType": "text/plain"}),
        ("empty_strings", {"displayName": "", "sourceType": "", "kind": ""}),
    ]
    for name, metadata in metadata_values:
        add(f"metadata_{name}", payload("metadata must not change text", metadata=metadata))

    invalid_cases = [
        ("top_level_array", [], "invalid_request"),
        ("top_level_string", "text", "invalid_request"),
        ("missing_document_id", {"text": "x", "metadata": {}, "chunking": {"chunkChars": 4, "chunkOverlap": 0}}, "invalid_document_id"),
        ("null_document_id", payload("x", document_id=None), "invalid_document_id"),
        ("empty_document_id", payload("x", document_id=""), "invalid_document_id"),
        ("space_document_id", payload("x", document_id=" doc "), "invalid_document_id"),
        ("numeric_document_id", payload("x", document_id=7), "invalid_document_id"),
        ("missing_text", {"documentId": "doc", "metadata": {}, "chunking": {"chunkChars": 4, "chunkOverlap": 0}}, "invalid_text"),
        ("null_text", payload(None), "invalid_text"),
        ("numeric_text", payload(7), "invalid_text"),
        ("empty_text", payload(""), "invalid_text"),
        ("whitespace_text", payload(" \t\r\n "), "invalid_text"),
        ("metadata_array", payload("x", metadata=[]), "invalid_metadata"),
        ("metadata_string", payload("x", metadata="bad"), "invalid_metadata"),
        ("metadata_display_object", payload("x", metadata={"displayName": {"x": 1}}), "invalid_metadata"),
        ("metadata_path", payload("x", metadata={"absolutePath": "/tmp/file"}), "invalid_metadata"),
        ("metadata_temp_path", payload("x", metadata={"temporaryPath": "C:/tmp"}), "invalid_metadata"),
        ("metadata_sensitive_field_1", payload("x", metadata={"authorization": "Bearer x"}), "invalid_metadata"),
        ("metadata_sensitive_field_2", payload("x", metadata={"apiKey": "secret"}), "invalid_metadata"),
        ("metadata_sensitive_field_3", payload("x", metadata={"token": "secret"}), "invalid_metadata"),
        ("metadata_raw_bytes", payload("x", metadata={"rawFileBytes": "AAAA"}), "invalid_metadata"),
        ("missing_chunking", {"documentId": "doc", "text": "x", "metadata": {}}, "invalid_request"),
        ("chunking_array", {**payload("x"), "chunking": []}, "invalid_request"),
        ("chunk_size_zero", payload("x", chunk_chars=0, chunk_overlap=0), "invalid_chunk_size"),
        ("chunk_size_negative", payload("x", chunk_chars=-1, chunk_overlap=0), "invalid_chunk_size"),
        ("chunk_size_float", payload("x", chunk_chars=1.5, chunk_overlap=0), "invalid_chunk_size"),
        ("chunk_size_bool", payload("x", chunk_chars=True, chunk_overlap=0), "invalid_chunk_size"),
        ("overlap_negative", payload("x", chunk_chars=4, chunk_overlap=-1), "invalid_chunk_overlap"),
        ("overlap_float", payload("x", chunk_chars=4, chunk_overlap=1.5), "invalid_chunk_overlap"),
        ("overlap_bool", payload("x", chunk_chars=4, chunk_overlap=True), "invalid_chunk_overlap"),
        ("overlap_equal", payload("abcdef", chunk_chars=4, chunk_overlap=4), "chunk_overlap_too_large"),
        ("overlap_larger", payload("abcdef", chunk_chars=4, chunk_overlap=5), "chunk_overlap_too_large"),
        ("unknown_top_field", {**payload("x"), "extra": True}, "invalid_request"),
        ("top_upload_path", {**payload("x"), "uploadPath": "/tmp/file"}, "invalid_request"),
        ("unknown_chunking_field", {**payload("x"), "chunking": {"chunkChars": 4, "chunkOverlap": 0, "mode": "new"}}, "invalid_request"),
    ]
    for name, value, code in invalid_cases:
        add(f"invalid_{name}", value, code=code)

    add("generated_excessive_nesting", code="nesting_limit_exceeded", generate="excessive_nesting")
    add("generated_document_too_large", code="document_too_large", generate="document_too_large")
    add("generated_request_too_large", code="request_too_large", generate="request_too_large")
    add("generated_invalid_json", code="invalid_request", generate="invalid_json")

    add("hash_stable_a", payload("stable hash text", document_id="doc-a", chunk_chars=6, chunk_overlap=1))
    add("hash_stable_b", payload("stable hash text", document_id="doc-a", chunk_chars=6, chunk_overlap=1), assertions={"sameDocumentHashAs": "hash_stable_a"})
    add("hash_text_changed", payload("stable hash text changed", document_id="doc-a", chunk_chars=6, chunk_overlap=1), assertions={"differentDocumentHashFrom": "hash_stable_a"})
    add("hash_document_id_changed", payload("stable hash text", document_id="doc-b", chunk_chars=6, chunk_overlap=1), assertions={"sameDocumentHashAs": "hash_stable_a", "differentChunkIdsFrom": "hash_stable_a"})
    add("hash_metadata_changed", payload("stable hash text", document_id="doc-a", metadata={"displayName": "other.txt"}, chunk_chars=6, chunk_overlap=1), assertions={"sameDocumentHashAs": "hash_stable_a"})
    add("hash_crlf", payload("alpha\r\nbeta", document_id="doc-crlf", chunk_chars=8, chunk_overlap=1))
    add("hash_lf", payload("alpha\nbeta", document_id="doc-crlf", chunk_chars=8, chunk_overlap=1), assertions={"sameDocumentHashAs": "hash_crlf"})
    add("hash_chunk_config_changed", payload("stable hash text", document_id="doc-a", chunk_chars=8, chunk_overlap=2), assertions={"differentDocumentHashFrom": "hash_stable_a"})
    add("hash_unicode_stable_a", payload("中文🚀 hash", document_id="doc-unicode", chunk_chars=4, chunk_overlap=1))
    add("hash_unicode_stable_b", payload("中文🚀 hash", document_id="doc-unicode", chunk_chars=4, chunk_overlap=1), assertions={"sameDocumentHashAs": "hash_unicode_stable_a"})

    names = [str(case["name"]) for case in cases]
    if len(cases) < 120 or len(names) != len(set(names)):
        raise RuntimeError(f"invalid corpus: total={len(cases)}, unique={len(set(names))}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"schemaVersion": 1, "cases": cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases to {OUTPUT}")


if __name__ == "__main__":
    main()
