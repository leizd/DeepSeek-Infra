import type { CSSProperties } from "react";

import type { TraceSpan } from "../../api/traceApi";
import {
  buildTraceSpanTree,
  formatTraceDuration,
  formatTraceNumber,
  isCacheHit,
  spanCategory,
} from "./traceSelectors";

function spanMeta(span: TraceSpan): string {
  return [
    span.kind,
    span.status,
    formatTraceDuration(span.durationMs),
    span.totalTokens ? `${formatTraceNumber(span.totalTokens)} tokens` : "",
    span.cacheHitRate ? `cache ${span.cacheHitRate}%` : isCacheHit(span) ? "cache hit" : "",
  ].filter(Boolean).join(" / ");
}

export function TraceSpanTree({ spans }: { spans: readonly TraceSpan[] }) {
  const tree = buildTraceSpanTree(spans);
  return (
    <section className="trace-section trace-tree-section" aria-labelledby="trace-span-tree-title">
      <div className="trace-section-heading">
        <h2 id="trace-span-tree-title">Span tree</h2>
        <span>{formatTraceNumber(spans.length)} spans</span>
      </div>
      {!tree.length ? (
        <p className="trace-empty">No spans recorded.</p>
      ) : (
        <div className="trace-tree" role="list">
          {tree.map(({ span, depth }) => (
            <article
              className="trace-tree-row"
              style={{ "--trace-depth": Math.min(depth, 8) } as CSSProperties}
              role="listitem"
              key={span.spanId || `${span.name}-${span.offsetMs}`}
            >
              <div className="trace-tree-title">
                <span className={`trace-kind-dot is-${spanCategory(span)}`} aria-hidden="true" />
                <strong>{span.name || span.kind || "span"}</strong>
              </div>
              <span className="trace-subtle">{spanMeta(span)}</span>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
