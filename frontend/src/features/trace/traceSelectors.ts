import type { TraceDetail, TraceSpan } from "../../api/traceApi";

export type TraceCategory = "agent" | "tool" | "rag" | "llm" | "cache" | "other";

export interface TraceTreeEntry {
  span: TraceSpan;
  depth: number;
}

export interface TraceCategorySummary {
  key: TraceCategory;
  label: string;
  count: number;
  durationMs: number;
  tokens: number;
  cacheHits: number;
  errors: number;
}

const CATEGORY_ORDER: readonly TraceCategory[] = ["agent", "tool", "rag", "llm", "cache", "other"];

export const TRACE_CATEGORY_LABELS: Record<TraceCategory, string> = {
  agent: "Agent",
  tool: "Tool / MCP",
  rag: "RAG",
  llm: "LLM",
  cache: "Cache",
  other: "Other",
};

export function buildTraceSpanTree(spans: readonly TraceSpan[]): TraceTreeEntry[] {
  const byId = new Map<string, TraceSpan>();
  for (const span of spans) {
    if (span.spanId) byId.set(span.spanId, span);
  }

  const childrenByParent = new Map<string, TraceSpan[]>();
  const roots: TraceSpan[] = [];
  for (const span of spans) {
    const parentId =
      span.parentSpanId && byId.has(span.parentSpanId) && span.parentSpanId !== span.spanId ? span.parentSpanId : "";
    if (!parentId) {
      roots.push(span);
      continue;
    }
    const children = childrenByParent.get(parentId) ?? [];
    children.push(span);
    childrenByParent.set(parentId, children);
  }

  const ordered: TraceTreeEntry[] = [];
  const visited = new Set<string>();
  const walk = (span: TraceSpan, depth: number) => {
    if (visited.has(span.spanId)) return;
    visited.add(span.spanId);
    ordered.push({ span, depth });
    const children = (childrenByParent.get(span.spanId) ?? []).slice().sort((a, b) => a.offsetMs - b.offsetMs);
    for (const child of children) walk(child, depth + 1);
  };

  for (const root of roots.slice().sort((a, b) => a.offsetMs - b.offsetMs)) walk(root, 0);
  for (const span of spans) {
    if (!visited.has(span.spanId)) ordered.push({ span, depth: 0 });
  }
  return ordered;
}

export function spanCategory(span: TraceSpan): TraceCategory {
  const source = `${span.kind} ${span.name}`.toLowerCase();
  if (source.includes("agent")) return "agent";
  if (["rag", "retriev", "citation", "file"].some((value) => source.includes(value))) return "rag";
  if (["llm", "deepseek", "openai", "model"].some((value) => source.includes(value))) return "llm";
  if (source.includes("cache")) return "cache";
  if (["tool", "mcp", "search", "fetch"].some((value) => source.includes(value))) return "tool";
  return "other";
}

export function isCacheHit(span: TraceSpan): boolean {
  return span.status.toLowerCase() === "hit" || (span.cacheHitRate ?? 0) > 0 || span.cacheHit;
}

export function isErrorSpan(span: TraceSpan): boolean {
  const status = span.status.toLowerCase();
  return Boolean(span.error) || Boolean(status && !["ok", "hit", "miss", "skipped", "completed", "running"].includes(status));
}

export function summarizeByCategory(spans: readonly TraceSpan[]): TraceCategorySummary[] {
  const summaries = new Map<TraceCategory, TraceCategorySummary>(
    CATEGORY_ORDER.map((key) => [key, {
      key,
      label: TRACE_CATEGORY_LABELS[key],
      count: 0,
      durationMs: 0,
      tokens: 0,
      cacheHits: 0,
      errors: 0,
    }]),
  );
  for (const span of spans) {
    const summary = summaries.get(spanCategory(span));
    if (!summary) continue;
    summary.count += 1;
    summary.durationMs += span.durationMs;
    summary.tokens += span.totalTokens;
    if (isCacheHit(span)) summary.cacheHits += 1;
    if (isErrorSpan(span)) summary.errors += 1;
  }
  return CATEGORY_ORDER.flatMap((key) => {
    const summary = summaries.get(key);
    return summary && summary.count > 0 ? [summary] : [];
  });
}

export function traceErrors(trace: TraceDetail): Array<{ name: string; error: string }> {
  const errors = trace.error ? [{ name: "trace", error: trace.error }] : [];
  for (const span of trace.spans) {
    if (isErrorSpan(span)) errors.push({ name: span.name || span.kind || "span", error: span.error || span.status || "error" });
  }
  return errors;
}

export function formatTraceDuration(ms: number | undefined): string {
  const value = Math.max(0, Math.round(Number(ms) || 0));
  if (value < 1_000) return `${value}ms`;
  const seconds = value / 1_000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

export function formatTraceNumber(value: number | undefined): string {
  return new Intl.NumberFormat("en-US").format(Math.max(0, Math.round(Number(value) || 0)));
}

export function traceWindowText(spans: readonly TraceSpan[]): string {
  const maxEnd = Math.max(0, ...spans.map((span) => span.offsetMs + span.durationMs));
  return maxEnd ? `0ms to ${formatTraceDuration(maxEnd)}` : "";
}
