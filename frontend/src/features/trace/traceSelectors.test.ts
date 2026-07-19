import { describe, expect, it } from "vitest";

import type { TraceSpan } from "../../api/traceApi";
import { spanCategory, summarizeByCategory, traceErrors } from "./traceSelectors";

function span(overrides: Partial<TraceSpan>): TraceSpan {
  return {
    spanId: "span",
    parentSpanId: "",
    name: "span",
    kind: "",
    status: "ok",
    offsetMs: 0,
    durationMs: 10,
    totalTokens: 0,
    cacheHitRate: null,
    cacheHit: false,
    error: "",
    ...overrides,
  };
}

describe("trace selectors", () => {
  it("classifies and summarizes trace spans", () => {
    const spans = [
      span({ spanId: "a", name: "agent plan", durationMs: 30, totalTokens: 20 }),
      span({ spanId: "b", name: "MCP fetch", status: "error", durationMs: 12, error: "timeout" }),
      span({ spanId: "c", name: "semantic cache", status: "hit", cacheHit: true }),
    ];
    expect(spans.map(spanCategory)).toEqual(["agent", "tool", "cache"]);
    expect(summarizeByCategory(spans)).toMatchObject([
      { key: "agent", count: 1, tokens: 20 },
      { key: "tool", count: 1, errors: 1 },
      { key: "cache", count: 1, cacheHits: 1 },
    ]);
  });

  it("combines trace-level and span-level errors", () => {
    const errors = traceErrors({
      traceId: "t1",
      title: "",
      kind: "chat",
      status: "error",
      startedAt: "",
      completedAt: "",
      durationMs: 10,
      error: "trace failed",
      summary: { spanCount: 1, totalTokens: 0, slowestSpan: "", slowestDurationMs: 0 },
      spans: [span({ name: "tool", status: "error", error: "tool failed" })],
    });
    expect(errors).toEqual([
      { name: "trace", error: "trace failed" },
      { name: "tool", error: "tool failed" },
    ]);
  });
});
