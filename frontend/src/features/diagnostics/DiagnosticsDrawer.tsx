import { useEffect, useState } from "react";

import { getTrace, buildTraceSpanTree, formatTraceDuration, isErrorSpan, traceExportUrl, type TraceDetail } from "../../api/traceApi";
import { useChat } from "../../contexts/ChatContext";
import { useDiagnostics } from "../../contexts/DiagnosticsContext";
import type { ChatMessage } from "../../domain/chat/types";
import { buildDiagnosticsRows } from "./diagnosticsRows";

function DiagnosticsRows({ message }: { message: ChatMessage }) {
  const rows = buildDiagnosticsRows(message);
  if (!rows.length) return <p className="history-empty">这条回复暂无诊断信息。</p>;
  return (
    <div className="diagnostics-list">
      {rows.map((row) => (
        <div className="diagnostics-row" key={row.label}>
          <span>{row.label}</span>
          <strong>{row.value}</strong>
        </div>
      ))}
    </div>
  );
}

function TraceWaterfall({ traceId }: { traceId: string }) {
  const [trace, setTrace] = useState<TraceDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setTrace(null);
    setError("");
    getTrace(traceId)
      .then((detail) => {
        if (!cancelled) setTrace(detail);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error && reason.message ? reason.message : "Trace 加载失败");
      });
    return () => {
      cancelled = true;
    };
  }, [traceId]);

  if (error) {
    return (
      <div className="diagnostics-list">
        <div className="diagnostics-row"><span>Trace ID</span><strong>{traceId}</strong></div>
        <div className="diagnostics-row"><span>错误</span><strong>{error}</strong></div>
      </div>
    );
  }
  if (!trace) {
    return (
      <div className="diagnostics-list">
        <div className="diagnostics-row"><span>Trace ID</span><strong>{traceId}</strong></div>
        <div className="diagnostics-row"><span>Status</span><strong>加载中…</strong></div>
      </div>
    );
  }

  const maxEnd = trace.spans.reduce((max, span) => Math.max(max, span.offsetMs + span.durationMs), 1);
  const tree = buildTraceSpanTree(trace.spans);
  return (
    <>
      <div className="diagnostics-list">
        <div className="diagnostics-row"><span>Trace ID</span><strong>{trace.traceId || traceId}</strong></div>
        <div className="diagnostics-row"><span>Status</span><strong>{trace.status}</strong></div>
        <div className="diagnostics-row"><span>Duration</span><strong>{formatTraceDuration(trace.durationMs)}</strong></div>
        <div className="diagnostics-row"><span>Spans</span><strong>{trace.summary.spanCount}</strong></div>
        {trace.summary.totalTokens > 0 && (
          <div className="diagnostics-row"><span>Total tokens</span><strong>{trace.summary.totalTokens}</strong></div>
        )}
        {trace.summary.slowestSpan && (
          <div className="diagnostics-row">
            <span>Slowest</span>
            <strong>{trace.summary.slowestSpan} · {formatTraceDuration(trace.summary.slowestDurationMs)}</strong>
          </div>
        )}
        <div className="diagnostics-row">
          <span>导出</span>
          <strong><a className="trace-export-link" href={traceExportUrl(traceId)} target="_blank" rel="noopener noreferrer">Export JSON</a></strong>
        </div>
      </div>
      {!tree.length ? (
        <p className="history-empty">No trace spans recorded yet.</p>
      ) : (
        <div className="trace-waterfall">
          {tree.map(({ span, depth }) => {
            const left = Math.min(98, Math.max(0, (span.offsetMs / maxEnd) * 100));
            const width = Math.max(2, Math.min(100 - left, (Math.max(1, span.durationMs) / maxEnd) * 100));
            const details = [
              span.totalTokens ? `${span.totalTokens} tokens` : "",
              span.cacheHitRate !== null ? `cache ${span.cacheHitRate}%` : "",
              span.error ? `error: ${span.error}` : "",
            ].filter(Boolean).join(" · ");
            return (
              <article
                className={isErrorSpan(span) ? "trace-span is-error" : "trace-span"}
                style={depth ? { marginLeft: `${Math.min(depth, 6) * 14}px` } : undefined}
                key={span.spanId || `${span.name}-${span.offsetMs}`}
              >
                <div className="trace-span-header">
                  <strong>{span.name}</strong>
                  <span>{[span.kind, span.status, formatTraceDuration(span.durationMs)].filter(Boolean).join(" · ")}</span>
                </div>
                <div className="trace-span-rail">
                  <div className="trace-span-bar" style={{ marginLeft: `${left}%`, width: `${width}%` }} />
                </div>
                {details && <div className="trace-span-details">{details}</div>}
              </article>
            );
          })}
        </div>
      )}
    </>
  );
}

export function DiagnosticsDrawer() {
  const diagnostics = useDiagnostics();
  const chat = useChat();
  const message = chat.messages.find((item) => item.id === diagnostics.target?.messageId) ?? null;
  if (!diagnostics.target || !message) return null;
  const traceId = typeof message.diagnostics?.traceId === "string" ? message.diagnostics.traceId : "";

  return (
    <section className="settings-drawer workspace-drawer" role="dialog" aria-modal="true" aria-label="诊断">
      <div className="drawer-heading">
        <div>
          <p className="eyebrow">{diagnostics.target.mode === "trace" ? "TRACE" : "DIAGNOSTICS"}</p>
          <h2>{diagnostics.target.mode === "trace" ? "Trace" : "诊断"}</h2>
        </div>
        <button type="button" aria-label="关闭诊断面板" onClick={diagnostics.closeDiagnostics}>×</button>
      </div>
      <div className="diagnostics-body">
        {diagnostics.target.mode === "rows" && <DiagnosticsRows message={message} />}
        {diagnostics.target.mode === "trace" && (
          traceId ? <TraceWaterfall traceId={traceId} /> : <p className="history-empty">这条消息没有 Trace。</p>
        )}
      </div>
    </section>
  );
}
