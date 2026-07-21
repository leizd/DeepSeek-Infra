import { lazy, Suspense } from "react";

import { useChat } from "../../contexts/ChatContext";
import { useDiagnostics } from "../../contexts/DiagnosticsContext";
import type { ChatMessage } from "../../domain/chat/types";
import { Icon } from "../../shared/ui/Icon";
import { buildDiagnosticsRows } from "./diagnosticsRows";

const TraceDetailView = lazy(() =>
  import("../trace/TraceDetailView").then((module) => ({
    default: module.TraceDetailView,
  })),
);

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
        <button type="button" aria-label="关闭诊断面板" onClick={diagnostics.closeDiagnostics}><Icon name="close" /></button>
      </div>
      <div className="diagnostics-body">
        {diagnostics.target.mode === "rows" && <DiagnosticsRows message={message} />}
        {diagnostics.target.mode === "trace" && (
          traceId ? (
            <Suspense fallback={<div className="trace-state trace-state--drawer" role="status"><strong>Loading trace...</strong></div>}>
              <TraceDetailView traceId={traceId} variant="drawer" />
            </Suspense>
          ) : <p className="history-empty">这条消息没有 Trace。</p>
        )}
      </div>
    </section>
  );
}
