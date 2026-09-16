# Gateway OpenAI SSE parity

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Status: **locally verified byte-identical**. Not yet exercised by exact-head CI.

This document records what the native Rust gateway reproduces of the Python
gateway's streaming `POST /v1/chat/completions` surface, how that was measured,
and what deliberately remains on the Python path.

## The two contracts

The streaming surface is **two** wire formats behind one route name, and mixing
them up is the main hazard in this area:

| Route | Format | Media type | Terminator | Event vocabulary |
| --- | --- | --- | --- | --- |
| `/v1/chat/completions` | OpenAI `chat.completion.chunk` SSE | `text/event-stream; charset=utf-8` | `data: [DONE]\n\n` | `role`, `content`, `finish_reason`, `error` |
| `/api/chat` | internal NDJSON | `application/x-ndjson; charset=utf-8` | connection close | `content`, `reasoning`, `search`, `system_note`, `done`, `error`, `memory_suggestion`, `agent*` |

This slice covers **only the first**. `/api/chat` (NDJSON) is **not** migrated and
is reported as a blocker rather than approximated.

## Oracle

Both halves come from real Python source, and the native side mirrors each:

| Half | Python oracle | Rust |
| --- | --- | --- |
| Upstream SSE decoding | `deepseek_client.stream_deepseek` (loop at `deepseek_client.py:2051-2116`) | `chat_stream::decode_event` / `decode_chunk` |
| Downstream SSE encoding | `openai_api._sse` + `openai_chat_stream` (`openai_api.py:93-130`) | `chat_stream::StreamChunkEncoder` |
| Event-to-frame mapping | `openai_chat_stream`'s `chunk()` closure | `chat_stream::forward_line` |
| Upstream request | `build_deepseek_request(..., stream=True)` → `{"model", "messages", "stream"}` | `request_preparation` + `chat_execution::open_chat_stream` |

### Decoding rules mirrored

From `stream_deepseek`'s loop, in order:

1. a blank line resets `event_name` to `"message"`;
2. `event: <name>` sets it (empty name falls back to `"message"`);
3. any line not starting with `data:` is skipped (comments, `id:`);
4. `data: [DONE]` terminates;
5. `event_name == "error"` routes through `sse_error_message` → the provider's
   own `error.message`, then `error.type`, then a top-level `message`/`type`,
   then the raw frame;
6. malformed JSON is logged and skipped, never aborting the stream;
7. reasoning is read as `reasoning_content` → `reasoning` → `thinking_content`
   → `thinking`, in that precedence;
8. then `delta.content`.

### Encoding rules mirrored

`_sse` writes `b"data: " + compact_json + b"\n\n"` with `ensure_ascii=False`.
The envelope key order is `id`, `object`, `created`, `model`, `choices`, and each
choice is `{index, delta, finish_reason}`.

**Key order is load-bearing and is why the frame is built by hand.** This crate
compiles `serde_json` without `preserve_order`, so its maps are key-sorted and a
`json!({...})` envelope would emit `choices` before `created`. Enabling
`preserve_order` workspace-wide would silently reorder every other Rust response,
so `StreamChunkEncoder::chunk` emits the fields in explicit order instead. The
byte-exact unit test pins this.

## Measurements

### Byte-identical frame probe

```bash
python tasks/native-runtime/sse_parity_probe.py > python.frames
cd rust && cargo run -p deepseek-gateway --example sse_parity_probe > ../rust.frames
```

Both sides replay the same two upstream scripts (`happy`, `error`) and print each
frame as hex, so trailing-newline and non-ASCII differences cannot be hidden by
terminal rendering. The Python side extracts `_sse` and `openai_completion_response`
from `openai_api.py` with `ast` and replays `stream_deepseek`'s decoding rules; it
does not import the gateway and does not reimplement the encoder.

Result, after normalizing the `\r\n` that Python's text-mode stdout adds on
Windows:

```
md5  python.frames (normalized)  = b9129475b6bae8b1239f4529e0a50932
md5  rust.frames                = b9129475b6bae8b1239f4529e0a50932
diff → no differences
```

12 frames across 2 cases, byte-identical.

### Real-boundary tests

`rust/crates/deepseek-gateway/tests/chat_stream.rs` drives `create_app()` through
Tower against a loopback upstream that writes actual `text/event-stream` bytes:

| Test | Contract pinned |
| --- | --- |
| `streaming_route_emits_the_oracle_frame_sequence` | frame order, separators, unescaped non-ASCII, reasoning dropped, `stream:true` + `Accept: text/event-stream` sent upstream |
| `streaming_reassembles_a_multibyte_character_split_across_chunks` | a character split across chunk boundaries survives |
| `streaming_error_frame_matches_the_oracle_and_still_terminates` | `event: error` frame shape; `[DONE]` still emitted; partial content kept |
| `streaming_upstream_failure_surfaces_as_http_status_not_a_frame` | non-success upstream status is an HTTP status, not `200` + error frame |
| `streaming_refuses_a_tool_call_turn_instead_of_flattening_it` | a `tool_calls` turn fails loudly instead of emitting its prose |
| `streaming_is_forwarded_by_preparation_not_refused` | preparation forwards `stream: true` as a boolean |

`chat_stream.rs` additionally carries 15 unit tests over the decoding and encoding
matrix.

## Behavior deliberately NOT reproduced

Each of these stays on the Python path and is a reported blocker, never a silent
behavior change:

- **`/api/chat` NDJSON**, including the `system_note`, `search`,
  `memory_suggestion` and `agent*` event kinds.
- **Tool-call rounds.** A streaming turn ending in `tool_calls` is refused with
  the same `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` code the non-streaming path uses.
  The refusal travels in-band as an error frame, because the role frame is
  already on the wire by the time the tool round is seen.
- **Reasoning as a channel.** `openai_chat_stream` maps only `content`, so
  reasoning deltas are consumed and dropped. Surface reasoning is `/api/chat`'s
  job, which is out of scope here.
- Semantic cache, memory retrieval, context compression, model router, web
  search, `create_pptx`, scheduler leases, resiliency retries, trace/span
  emission and the budget ledger.

## Rollback

`request_preparation` no longer refuses `stream: true`, so the Rust route is the
only streaming path once traffic reaches it. Reverting this slice is a matter of
reverting the commit: no persisted state, no schema, no migration. The Python
route remains intact and is selected whenever the native gateway is not the
entry point, so a revert restores the previous behavior exactly.
