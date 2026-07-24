import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./app/App";
import { buildUpdateStore } from "./app/buildUpdateStore";
import { startPageLifecyclePersistence } from "./app/pageLifecycle";
import { startWorkspaceServiceWorkerRuntime } from "./app/serviceWorkerRegistration";
import "./shared/styles/app.css";
import "./shared/styles/workspace-drawer-frame.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("React root element is missing");
}

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);

startPageLifecyclePersistence({ windowValue: window, documentValue: document });

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void startWorkspaceServiceWorkerRuntime({
      container: navigator.serviceWorker,
      navigatorValue: navigator,
      windowValue: window,
      documentValue: document,
      pageBuildId: __APP_BUILD_ID__,
      buildUpdates: buildUpdateStore,
      fetchValue: window.fetch.bind(window),
      createBroadcastChannel: "BroadcastChannel" in window
        ? (name) => new BroadcastChannel(name)
        : undefined,
    }).catch(() => undefined);
  });
}
