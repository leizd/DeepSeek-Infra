import { Link, useParams } from "react-router-dom";

import { traceExportUrl } from "../../api/traceApi";
import { appRoutes } from "../../app/routes";
import { Icon } from "../../shared/ui/Icon";
import { TraceDetailView } from "./TraceDetailView";

export function TracePage() {
  const { traceId = "" } = useParams<{ traceId: string }>();
  return (
    <main className="trace-page">
      <nav className="trace-topbar" aria-label="Trace navigation">
        <Link className="trace-back-link" to={appRoutes.root}><Icon name="back" /> Chat</Link>
        <strong>DeepSeek Infra <span>/ Trace</span></strong>
        <a className="trace-export-button" href={traceExportUrl(traceId)} target="_blank" rel="noopener noreferrer">Export JSON</a>
      </nav>
      <TraceDetailView traceId={traceId} />
    </main>
  );
}
