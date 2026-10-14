import { lazy, Suspense, type ReactNode } from "react";
import { Route, Routes, useLocation } from "react-router-dom";

import { ChatPage } from "../features/chat/ChatPage";
import { BuildUpdateBanner } from "../features/build-update/BuildUpdateBanner";
import { AppProviders } from "./AppProviders";
import { NotFoundPage } from "./NotFoundPage";
import { RouteErrorBoundary } from "./RouteErrorBoundary";
import { RouteLoading } from "./RouteLoading";

const TracePage = lazy(() =>
  import("../features/trace/TracePage").then((module) => ({
    default: module.TracePage,
  })),
);

const LauncherPage = lazy(() => import("../features/launcher/LauncherPage").then((module) => ({ default: module.LauncherPage })));

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
	const location = useLocation();
	if (location.pathname === "/launcher" || location.pathname === "/launcher/") {
		return <RouteBoundary><Suspense fallback={<RouteLoading label="正在读取启动器…" />}><LauncherPage /></Suspense></RouteBoundary>;
	}
  return (
    <>
      <BuildUpdateBanner />
      <Routes>
        <Route path="/" element={<WorkspaceRoute />} />
        <Route path="/ui/" element={<WorkspaceRoute />} />
        <Route path="/trace/:traceId" element={<TraceRoute />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </>
  );
}
