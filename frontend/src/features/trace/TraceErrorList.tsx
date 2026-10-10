import type { TraceDetail } from "../../api/traceApi";
import { traceErrors } from "./traceSelectors";

export function TraceErrorList({ trace }: { trace: TraceDetail }) {
  const errors = traceErrors(trace);
  return (
    <section className="trace-section" aria-labelledby="trace-errors-title">
      <div className="trace-section-heading">
        <h2 id="trace-errors-title">Errors</h2>
        <span>{errors.length || "none"}</span>
      </div>
      {!errors.length ? (
        <p className="trace-empty">No errors recorded.</p>
      ) : (
        <div className="trace-error-list">
          {errors.map((item, index) => (
            <article className="trace-error-row" key={`${item.name}-${index}`}>
              <strong>{item.name}</strong>
              <span>{item.error}</span>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
