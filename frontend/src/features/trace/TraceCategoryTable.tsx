import type { TraceSpan } from "../../api/traceApi";
import { formatTraceDuration, formatTraceNumber, summarizeByCategory } from "./traceSelectors";

export function TraceCategoryTable({ spans }: { spans: readonly TraceSpan[] }) {
  const summaries = summarizeByCategory(spans);
  return (
    <section className="trace-section" aria-labelledby="trace-category-title">
      <div className="trace-section-heading">
        <h2 id="trace-category-title">Category summary</h2>
      </div>
      <div className="trace-table-wrap">
        <table className="trace-category-table">
          <thead>
            <tr><th>Type</th><th>Count</th><th>Duration</th><th>Tokens</th><th>Cache</th><th>Errors</th></tr>
          </thead>
          <tbody>
            {summaries.map((summary) => (
              <tr key={summary.key}>
                <td><span className={`trace-kind-dot is-${summary.key}`} aria-hidden="true" />{summary.label}</td>
                <td>{formatTraceNumber(summary.count)}</td>
                <td>{formatTraceDuration(summary.durationMs)}</td>
                <td>{formatTraceNumber(summary.tokens)}</td>
                <td>{formatTraceNumber(summary.cacheHits)}</td>
                <td>{formatTraceNumber(summary.errors)}</td>
              </tr>
            ))}
            {!summaries.length && <tr><td className="trace-empty-cell" colSpan={6}>No spans recorded.</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  );
}
