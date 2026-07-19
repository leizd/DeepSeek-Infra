import type { TraceDetail } from "../../api/traceApi";
import { formatTraceDuration, formatTraceNumber, isCacheHit } from "./traceSelectors";

function SummaryItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="trace-summary-item">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function TraceSummary({ trace }: { trace: TraceDetail }) {
  const cacheHits = trace.spans.filter(isCacheHit).length;
  return (
    <dl className="trace-summary" aria-label="Trace summary">
      <SummaryItem label="Duration" value={formatTraceDuration(trace.durationMs)} />
      <SummaryItem label="Spans" value={formatTraceNumber(trace.summary.spanCount || trace.spans.length)} />
      <SummaryItem label="Tokens" value={formatTraceNumber(trace.summary.totalTokens)} />
      <SummaryItem label="Slowest" value={trace.summary.slowestSpan || "none"} />
      <SummaryItem label="Cache hits" value={formatTraceNumber(cacheHits)} />
    </dl>
  );
}
