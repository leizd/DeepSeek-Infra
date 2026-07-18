import { describe, expect, it, vi } from "vitest";

import { HttpClient } from "./httpClient";
import { buildTraceSpanTree, formatTraceDuration, getTrace, isErrorSpan, normalizeTrace } from "./traceApi";
import { buildDiagnosticsRows, hasDiagnostics, traceIdForMessage } from "../features/diagnostics/diagnosticsRows";
import type { ChatMessage } from "../domain/chat/types";

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "回答",
    reasoning: "",
    createdAt: 1,
    phase: "done",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
    ...overrides,
  };
}

describe("traceApi", () => {
  it("normalizes the trace payload with summary fallbacks", () => {
    const trace = normalizeTrace({
      traceId: "t1",
      status: "ok",
      durationMs: 1200,
      spans: [{ spanId: "s1", name: "llm", durationMs: 900, offsetMs: 10 }],
    });
    expect(trace).toMatchObject({ traceId: "t1", status: "ok", durationMs: 1200 });
    expect(trace.summary.spanCount).toBe(1);
    expect(trace.spans[0]).toMatchObject({ spanId: "s1", name: "llm", durationMs: 900 });
  });

  it("builds the span tree with parents before children, sorted by offset", () => {
    const tree = buildTraceSpanTree([
      { spanId: "child", parentSpanId: "root", name: "child", kind: "", status: "ok", offsetMs: 50, durationMs: 10, totalTokens: 0, cacheHitRate: null, error: "" },
      { spanId: "root", parentSpanId: "", name: "root", kind: "", status: "ok", offsetMs: 0, durationMs: 100, totalTokens: 0, cacheHitRate: null, error: "" },
      { spanId: "orphan", parentSpanId: "missing", name: "orphan", kind: "", status: "ok", offsetMs: 25, durationMs: 5, totalTokens: 0, cacheHitRate: null, error: "" },
    ]);
    expect(tree.map((entry) => `${entry.span.spanId}:${entry.depth}`)).toEqual(["root:0", "child:1", "orphan:0"]);
  });

  it("guards cycles and duplicate visits", () => {
    const tree = buildTraceSpanTree([
      { spanId: "a", parentSpanId: "b", name: "a", kind: "", status: "ok", offsetMs: 0, durationMs: 1, totalTokens: 0, cacheHitRate: null, error: "" },
      { spanId: "b", parentSpanId: "a", name: "b", kind: "", status: "ok", offsetMs: 1, durationMs: 1, totalTokens: 0, cacheHitRate: null, error: "" },
    ]);
    expect(tree).toHaveLength(2);
  });

  it("formats durations and detects error spans", () => {
    expect(formatTraceDuration(400)).toBe("400ms");
    expect(formatTraceDuration(1200)).toBe("1.2s");
    expect(formatTraceDuration(61_000)).toBe("1m 1s");
    expect(formatTraceDuration(0)).toBe("");
    expect(isErrorSpan({ status: "error" } as never)).toBe(true);
    expect(isErrorSpan({ status: "ok" } as never)).toBe(false);
  });

  it("fetches the trace detail", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ trace: { traceId: "t9", spans: [] } }), { status: 200 }));
    const trace = await getTrace("t9", new HttpClient({ fetchImpl }));
    expect(String((fetchImpl.mock.calls[0] as unknown as [string])[0])).toBe("/api/traces/t9");
    expect(trace.traceId).toBe("t9");
  });
});

describe("diagnosticsRows", () => {
  it("builds rows only for present values", () => {
    const rows = buildDiagnosticsRows(message({
      diagnostics: {
        requestMessageCount: 4,
        memoryEnabled: true,
        memoryHitCount: 2,
        costUsd: 0.0023,
        traceId: "trace-1",
        agentCache: { hitTokens: 10, missTokens: 30, hitRate: 0.25 },
      },
      usage: { prompt_tokens: 100, total_tokens: 160 },
    }));
    const map = new Map(rows.map((row) => [row.label, row.value]));
    expect(map.get("请求消息数")).toBe("4");
    expect(map.get("长期记忆")).toBe("开启 · 命中 2 条");
    expect(map.get("本轮成本")).toBe("$0.0023");
    expect(map.get("Trace ID")).toBe("trace-1");
    expect(map.get("Agent 缓存命中率")).toBe("25%");
    expect(map.has("预算降级")).toBe(false);
  });

  it("falls back to search snapshot counts and exposes the trace id", () => {
    const withSearch = message({ search: { rounds: [{ round: 1 }], results: [{ title: "t", url: "u" }] } });
    const rows = buildDiagnosticsRows(withSearch);
    expect(rows.find((row) => row.label === "搜索轮数")?.value).toBe("1");
    expect(rows.find((row) => row.label === "搜索来源数")?.value).toBe("1");
    expect(hasDiagnostics(withSearch)).toBe(true);
    expect(traceIdForMessage(message({ diagnostics: { traceId: "t" } }))).toBe("t");
    expect(traceIdForMessage(message())).toBe("");
    expect(hasDiagnostics(message())).toBe(false);
  });
});
