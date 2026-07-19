import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { getTrace, traceExportUrl, type TraceDetail } from "../../api/traceApi";
import { appRoutes } from "../../app/routes";
import { TraceCategoryTable } from "./TraceCategoryTable";
import { TraceErrorList } from "./TraceErrorList";
import { TraceSpanTree } from "./TraceSpanTree";
import { TraceSummary } from "./TraceSummary";
import { TraceWaterfall } from "./TraceWaterfall";
import "./trace.css";

interface TraceDetailViewProps {
  traceId: string;
  variant?: "page" | "drawer";
}

export function TraceDetailView({ traceId, variant = "page" }: TraceDetailViewProps) {
  const [trace, setTrace] = useState<TraceDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setTrace(null);
    setError("");
    if (!traceId) {
      setError("Trace id is missing.");
      return () => controller.abort();
    }
    getTrace(traceId, { signal: controller.signal })
      .then(setTrace)
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error && reason.message ? reason.message : "Unable to load trace.");
      });
    return () => controller.abort();
  }, [traceId]);

  useEffect(() => {
    if (variant !== "page" || !trace) return;
    const previousTitle = document.title;
    document.title = `${trace.title || trace.traceId || "Trace"} - DeepSeek Infra Trace`;
    return () => {
      document.title = previousTitle;
    };
  }, [trace, variant]);

  if (error) {
    return (
      <div className={`trace-state trace-state--${variant}`} role="alert">
        <span>TRACE LOAD FAILED</span>
        <strong>{error}</strong>
        <code>{traceId}</code>
      </div>
    );
  }
  if (!trace) {
    return (
      <div className={`trace-state trace-state--${variant}`} role="status">
        <span>TRACE</span>
        <strong>Loading trace...</strong>
        <code>{traceId}</code>
      </div>
    );
  }

  if (variant === "drawer") {
    return (
      <div className="trace-detail trace-detail--drawer">
        <TraceSummary trace={trace} />
        <TraceWaterfall spans={trace.spans} />
        <div className="trace-drawer-actions">
          <Link to={appRoutes.trace(trace.traceId || traceId)}>Open full trace</Link>
          <a href={traceExportUrl(trace.traceId || traceId)} target="_blank" rel="noopener noreferrer">Export JSON</a>
        </div>
      </div>
    );
  }

  const subtitle = [trace.kind, trace.traceId, [trace.startedAt, trace.completedAt].filter(Boolean).join(" - ")]
    .filter(Boolean)
    .join(" / ");
  const hasError = Boolean(trace.error) || trace.status.toLowerCase() === "error";
  return (
    <div className="trace-detail trace-detail--page">
      <header className="trace-headline">
        <div>
          <p>TRACE</p>
          <h1>{trace.title || trace.traceId || "Trace"}</h1>
          <span>{subtitle}</span>
        </div>
        <span className={hasError ? "trace-status is-error" : "trace-status"}>{trace.status}</span>
      </header>
      <TraceSummary trace={trace} />
      <div className="trace-primary-grid">
        <TraceSpanTree spans={trace.spans} />
        <TraceWaterfall spans={trace.spans} />
      </div>
      <div className="trace-secondary-grid">
        <TraceCategoryTable spans={trace.spans} />
        <TraceErrorList trace={trace} />
      </div>
    </div>
  );
}
