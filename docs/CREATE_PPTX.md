# create_pptx (16:9 OOXML deck)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported, wired into the tool loop, content-model probe-verified locally.**
Exact-head CI has not run against this slice.

`deepseek-policy::presentations` mirrors `create_presentation` in
`infra/tool_runtime/presentations.py`. The **content model** is the oracle's:
title/slide refusals, `content` → bullets fallback, MD5 deck theme, layout
picker (quote/cards/process/comparison/summary/…), automatic agenda when there
are four or more content slides, outline, and the download note.

The `.pptx` **bytes** are not a python-pptx fingerprint. The native writer
emits a valid 16:9 OOXML zip (`PK` + `ppt/slides/slideN.xml`) with the same
titles, bullets and layouts, including CJK.

`create_presentation_from_text` / outline-from-markdown remains a **slides
skill** path, not this tool branch.

## Verification

- `tasks/native-runtime/pptx_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/pptx_parity_probe.rs`
  (md5 `d360f5e45f605284e51c13dbcdeba9ea`, 3676 chars)
- `chat_route_creates_a_pptx_deck` — the wired loop writes `.pptx` under
  `.generated` instead of answering `Tool did not run`
