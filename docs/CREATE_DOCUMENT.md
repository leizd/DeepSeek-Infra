# create_document (docx + PDF)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported, wired into the tool loop, content-model probe-verified locally.**
Exact-head CI has not run against this slice.

`deepseek-policy::documents` mirrors `infra/tool_runtime/documents.py`. The
**content model** is the oracle's: format aliases (`word`/`doc`/`docx`/`pdf`),
section and table normalization, MD5 theme, outline, and the download note.
The Office/PDF **bytes** are not python-docx / reportlab fingerprints — those
libraries are not a frozen protocol. The native writer produces:

- a valid OOXML zip (`PK` + `word/document.xml`) with CJK in the XML
- a valid PDF (`%PDF-`) that carries CJK as UCS-2 via `/STSong-Light` +
  `/UniGB-UCS2-H`, the same CID approach reportlab's `UnicodeCIDFont` uses

## What is byte-identical, what is not

Byte-identical: format aliases, refusals, ragged-table padding, MD5 theme
index, outline/note envelope (file ids excluded). Probe md5
`a09bbde438e3ca1e86f79c0c7e15c953`.

Not byte-identical: the `.docx` / `.pdf` files themselves. Tests assert
magic, zip membership, and that the title/headings survive in the payload.

## Verification

- `tasks/native-runtime/document_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/document_parity_probe.rs`
- `chat_route_creates_a_docx_document` — the wired loop writes `.docx`
  under `.generated` instead of answering `Tool did not run`
