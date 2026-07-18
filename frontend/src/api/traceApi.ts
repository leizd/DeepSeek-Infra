import { httpClient, type HttpClient } from "./httpClient";
import type { JsonRecord } from "../domain/chat/types";

export interface TraceSpan {
  spanId: string;
  parentSpanId: string;
  name: string;
  kind: string;
  status: string;
  offsetMs: number;
  durationMs: number;
  totalTokens: number;
  cacheHitRate: number | null;
  error: string;
}

export interface TraceSummary {
  spanCount: number;
  totalTokens: number;
  slowestSpan: string;
  slowestDurationMs: number;
}

export interface TraceDetail {
  traceId: string;
  status: string;
  durationMs: number;
  summary: TraceSummary;
  spans: TraceSpan[];
}

export interface TraceTreeEntry {
  span: TraceSpan;
  depth: number;
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

export function normalizeTraceSpan(raw: unknown): TraceSpan | null {
  if (!isRecord(raw)) return null;
  return {
    spanId: asString(raw.spanId),
    parentSpanId: asString(raw.parentSpanId),
    name: asString(raw.name) || asString(raw.kind) || "span",
    kind: asString(raw.kind),
    status: asString(raw.status),
    offsetMs: asNumber(raw.offsetMs),
    durationMs: asNumber(raw.durationMs),
    totalTokens: asNumber(raw.totalTokens),
    cacheHitRate: typeof raw.cacheHitRate === "number" ? raw.cacheHitRate : null,
    error: asString(raw.error),
  };
}

export function normalizeTrace(raw: unknown): TraceDetail {
  const record = isRecord(raw) ? raw : {};
  const summary = isRecord(record.summary) ? record.summary : {};
  const spans = Array.isArray(record.spans)
    ? record.spans.flatMap((span) => {
        const normalized = normalizeTraceSpan(span);
        return normalized ? [normalized] : [];
      })
    : [];
  return {
    traceId: asString(record.traceId),
    status: asString(record.status) || "unknown",
    durationMs: asNumber(record.durationMs),
    summary: {
      spanCount: asNumber(summary.spanCount) || spans.length,
      totalTokens: asNumber(summary.totalTokens),
      slowestSpan: asString(summary.slowestSpan),
      slowestDurationMs: asNumber(summary.slowestDurationMs),
    },
    spans,
  };
}

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
    } else {
      const children = childrenByParent.get(parentId) ?? [];
      children.push(span);
      childrenByParent.set(parentId, children);
    }
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

export function formatTraceDuration(ms: number | undefined): string {
  const value = asNumber(ms);
  if (!value) return "";
  if (value < 1_000) return `${Math.round(value)}ms`;
  if (value < 60_000) return `${(value / 1_000).toFixed(1)}s`;
  return `${Math.floor(value / 60_000)}m ${Math.round((value % 60_000) / 1_000)}s`;
}

const OK_STATUSES = new Set(["ok", "hit", "miss", "skipped", ""]);

export function isErrorSpan(span: TraceSpan): boolean {
  return !OK_STATUSES.has(span.status);
}

export async function getTrace(traceId: string, client: HttpClient = httpClient): Promise<TraceDetail> {
  const body = await client.json<{ trace?: unknown }>(`/api/traces/${encodeURIComponent(traceId)}`);
  return normalizeTrace(body.trace);
}

export function traceExportUrl(traceId: string): string {
  return `/api/traces/${encodeURIComponent(traceId)}/export.json`;
}
