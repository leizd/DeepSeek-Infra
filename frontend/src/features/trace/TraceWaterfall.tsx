import type { CSSProperties } from "react";

import type { TraceSpan } from "../../api/traceApi";
import {
  buildTraceSpanTree,
  formatTraceDuration,
  formatTraceNumber,
  isCacheHit,
  isErrorSpan,
  spanCategory,
  traceWindowText,
  TRACE_CATEGORY_LABELS,
} from "./traceSelectors";

function spanMetrics(span: TraceSpan): string {
  return [
    formatTraceDuration(span.durationMs),
    span.totalTokens ? `${formatTraceNumber(span.totalTokens)} tokens` : "",
    span.cacheHitRate ? `cache ${span.cacheHitRate}%` : isCacheHit(span) ? "cache hit" : "",
  ].filter(Boolean).join(" / ");
}

export function TraceWaterfall({ spans }: { spans: readonly TraceSpan[] }) {
  const tree = buildTraceSpanTree(spans);
  const maxEnd = Math.max(1, ...tree.map(({ span }) => span.offsetMs + Math.max(1, span.durationMs)));
  return (
    <section className="trace-section trace-waterfall-section" aria-labelledby="trace-waterfall-title">
      <div className="trace-section-heading">
        <h2 id="trace-waterfall-title">Waterfall</h2>
        <span>{traceWindowText(spans)}</span>
      </div>
      {!tree.length ? (
        <p className="trace-empty">No spans recorded.</p>
      ) : (
        <div className="trace-waterfall-grid">
          {tree.map(({ span, depth }) => {
            const category = spanCategory(span);
            const left = Math.min(98, Math.max(0, (span.offsetMs / maxEnd) * 100));
            const width = Math.max(1, Math.min(100 - left, (Math.max(1, span.durationMs) / maxEnd) * 100));
            return (
              <article
                className={isErrorSpan(span) ? "trace-waterfall-row is-error" : "trace-waterfall-row"}
                key={span.spanId || `${span.name}-${span.offsetMs}`}
              >
                <div className="trace-waterfall-name" style={{ "--trace-depth": Math.min(depth, 8) } as CSSProperties}>
                  <strong>{span.name || span.kind || "span"}</strong>
                  <span>{[TRACE_CATEGORY_LABELS[category], span.kind, span.status].filter(Boolean).join(" / ")}</span>
                </div>
                <div className="trace-waterfall-lane" role="img" aria-label={`${span.name} ${spanMetrics(span)}`}>
                  <span className={`trace-waterfall-bar is-${category}`} style={{ left: `${left}%`, width: `${width}%` }} />
                </div>
                <span className="trace-waterfall-metrics">{spanMetrics(span)}</span>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
