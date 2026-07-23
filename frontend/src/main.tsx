import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./app/App";
import { scheduleWorkspaceOfflineWarmup } from "./app/workspaceOfflineWarmup";
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

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    const atRoot = !window.location.pathname.startsWith("/ui/");
    navigator.serviceWorker
      .register(atRoot ? "/sw.js" : "/ui/sw.js", { scope: atRoot ? "/" : "/ui/" })
      .then(() => navigator.serviceWorker.ready)
      .then((registration) => {
        scheduleWorkspaceOfflineWarmup(registration, navigator, window);
      })
      .catch(() => undefined);
  });
}
