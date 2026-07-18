import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import { AppProviders } from "./app/AppProviders";
import "./shared/styles/app.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("React root element is missing");
}

createRoot(root).render(
  <StrictMode>
    <AppProviders>
      <App />
    </AppProviders>
  </StrictMode>,
);

if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    const atRoot = !window.location.pathname.startsWith("/ui/");
    navigator.serviceWorker
      .register(atRoot ? "/sw.js" : "/ui/sw.js", { scope: atRoot ? "/" : "/ui/" })
      .catch(() => undefined);
  });
}
