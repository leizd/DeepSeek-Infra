# create_mindmap (SVG layout + generated-file store)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported, wired into the tool loop, SVG byte-verified locally.**
Exact-head CI has not run against this slice.

`deepseek-policy::mindmaps` mirrors `infra/tool_runtime/mindmaps.py`. Layout,
tokenization, wrapping and SVG rendering are pure. Persistence goes through
`generated_files`, which writes `<root>/.generated/{id}.svg` and returns
`/api/download?id=…`.

This is the first media tool that can be proven native without python-docx,
python-pptx or reportlab. Those two remaining generators still need Office/PDF
libraries.

## Generated files are unique-id creates

`.generated` is not a durable read-modify-write store. Each write uses a 32-hex
id from `Entropy::new_file_id` (`secrets.token_hex(16)`). Cleanup only unlinks
names older than six hours. That is why this is not a declared ownership domain
the way `memory_store` / `reminders_store` are.

## Verification

- `tasks/native-runtime/mindmap_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/mindmap_parity_probe.rs`
- Probe includes the full SVG for the sample outline, XML escaping, and
  `title`/`name` aliases
- `chat_route_creates_a_mindmap_svg` — the wired loop writes an `.svg` under
  `.generated` instead of answering `Tool did not run`
