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
  cacheHit: boolean;
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
  title: string;
  kind: string;
  status: string;
  startedAt: string;
  completedAt: string;
  durationMs: number;
  error: string;
  summary: TraceSummary;
  spans: TraceSpan[];
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
  const diagnostics = isRecord(raw.diagnostics) ? raw.diagnostics : {};
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
    cacheHit: diagnostics.cacheHit === true,
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
    title: asString(record.title),
    kind: asString(record.kind),
    status: asString(record.status) || "unknown",
    startedAt: asString(record.startedAt),
    completedAt: asString(record.completedAt),
    durationMs: asNumber(record.durationMs),
    error: asString(record.error),
    summary: {
      spanCount: asNumber(summary.spanCount) || spans.length,
      totalTokens: asNumber(summary.totalTokens),
      slowestSpan: asString(summary.slowestSpan),
      slowestDurationMs: asNumber(summary.slowestDurationMs),
    },
    spans,
  };
}

export async function getTrace(traceId: string, client: HttpClient = httpClient): Promise<TraceDetail> {
  const body = await client.json<{ trace?: unknown }>(`/api/traces/${encodeURIComponent(traceId)}`);
  return normalizeTrace(body.trace);
}

export function traceExportUrl(traceId: string): string {
  return `/api/traces/${encodeURIComponent(traceId)}/export.json`;
}
