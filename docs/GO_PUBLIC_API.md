# Go public `/api` (control-plane edge)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **control-plane subset on `deepseekd`.** Production HTTP is still
Python. The native gateway forwards `/api/*` only when `GO_CONTROL_ADDR` /
`DEEPSEEK_GO_CONTROL_URL` is set.

## What Go serves

| Path | Method | Behavior |
| --- | --- | --- |
| `/healthz` | any | Shadow status (`ok`, `mode`, `mutationAuthority`, `productionMutation`, `shadowStore`) |
| `/api/control/status` | GET | Same JSON as `/healthz` |
| `/api/config` | GET | Go-owned subset: `owner=go`, version, runtime, `hasServerKey`/`hasSearch` booleans, default model, searchModes, mcp/a2a hub flags. **Not** Python's OCR/RAG/budget/toolPolicy blob. Never echoes tokens. |
| `/api/mcp` | GET | Native hub flags (`protocolVersion` 2025-06-18, `externalBridge: false`) |
| `/api/a2a` | GET | Native hub flags (`protocolVersion` 0.3.0, `streaming: true`); restart-safe tasks require the separate mTLS A2A control configuration described in `A2A_HUB.md` |
| `/api/cutover/status` | GET | Cutover record for `?domain=`; `domain` required |
| other `/api/*` | any | `501` `GO_API_NOT_IMPLEMENTED` |

Mutating cutover (`POST /internal/cutover/transition`) stays on `/internal`.
Public `/api/cutover/transition` is **not** registered.

## Honest gaps

- Python still owns production `/api/*` (full config, chat, tools, …).
- Native gateway without `GO_CONTROL_ADDR` still answers
  `GO_CONTROL_PROXY_NOT_READY`.
- Unimplemented public paths are 501, not a fake success.
