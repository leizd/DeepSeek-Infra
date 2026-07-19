import { lazy, Suspense, type ReactNode } from "react";
import { Route, Routes, useLocation } from "react-router-dom";

import { ChatPage } from "../features/chat/ChatPage";
import { AppProviders } from "./AppProviders";
import { NotFoundPage } from "./NotFoundPage";
import { RouteErrorBoundary } from "./RouteErrorBoundary";
import { RouteLoading } from "./RouteLoading";

const TracePage = lazy(() =>
  import("../features/trace/TracePage").then((module) => ({
    default: module.TracePage,
  })),
);

function RouteBoundary({ children }: { children: ReactNode }) {
  const location = useLocation();
  return <RouteErrorBoundary key={location.pathname}>{children}</RouteErrorBoundary>;
}

function WorkspaceRoute() {
  return (
    <RouteBoundary>
      <AppProviders>
        <ChatPage />
      </AppProviders>
    </RouteBoundary>
  );
}

function TraceRoute() {
  return (
    <RouteBoundary>
      <Suspense fallback={<RouteLoading label="Loading trace workspace..." />}>
        <TracePage />
      </Suspense>
    </RouteBoundary>
  );
}

export function App() {
  return (
    <Routes>
      <Route path="/" element={<WorkspaceRoute />} />
      <Route path="/ui/" element={<WorkspaceRoute />} />
      <Route path="/trace/:traceId" element={<TraceRoute />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
