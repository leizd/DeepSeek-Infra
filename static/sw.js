const CACHE_PREFIX = "deepseek-infra-";
const CACHE_NAME = "deepseek-infra-v402";
const CORE_SHELL = [
  "/index.html",
  "/theme_boot.js",
  "/vendor/inter/inter.css",
  "/vendor/inter/Inter-Variable.ttf",
  "/styles.css",
  "/vaultr-brutalist.css",
  "/vendor/katex/katex.min.css",
  "/vendor/katex/katex.min.js",
  "/math_core.js",
  "/seek_core.js",
  "/modules/network.js",
  "/modules/upload_controller.js",
  "/modules/credential_store.js",
  "/modules/workspace_tabs.js",
  "/modules/charts.js",
  "/modules/format.js",
  "/modules/markdown.js",
  "/modules/normalize.js",
  "/modules/settings.js",
  "/modules/panels.js",
  "/modules/skills.js",
  "/modules/skill_builder.js",
  "/modules/reminder_parse.js",
  "/modules/speech_text.js",
  "/modules/stream.js",
  "/modules/agent_timeline.js",
  "/modules/chat.js",
  "/app.js",
  "/manifest.webmanifest",
];
const OPTIONAL_SHELL = [
  "/",
  "/trace_viewer.html",
  "/modules/trace_waterfall.js",
  "/modules/trace_viewer.js",
  "/gemini.css",
  "/favicon.ico",
  "/icons/apple-touch-icon.png",
  "/icons/badge-96x96.png",
  "/icons/favicon-16x16.png",
  "/icons/favicon-32x32.png",
  "/icons/favicon.svg",
  "/icons/icon.svg",
  "/icons/maskable-192x192.png",
  "/icons/maskable-512x512.png",
  "/icons/pwa-192x192.png",
  "/icons/pwa-512x512.png",
  "/vendor/katex/fonts/KaTeX_AMS-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Caligraphic-Bold.woff2",
  "/vendor/katex/fonts/KaTeX_Caligraphic-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Fraktur-Bold.woff2",
  "/vendor/katex/fonts/KaTeX_Fraktur-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Main-Bold.woff2",
  "/vendor/katex/fonts/KaTeX_Main-BoldItalic.woff2",
  "/vendor/katex/fonts/KaTeX_Main-Italic.woff2",
  "/vendor/katex/fonts/KaTeX_Main-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Math-BoldItalic.woff2",
  "/vendor/katex/fonts/KaTeX_Math-Italic.woff2",
  "/vendor/katex/fonts/KaTeX_SansSerif-Bold.woff2",
  "/vendor/katex/fonts/KaTeX_SansSerif-Italic.woff2",
  "/vendor/katex/fonts/KaTeX_SansSerif-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Script-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Size1-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Size2-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Size3-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Size4-Regular.woff2",
  "/vendor/katex/fonts/KaTeX_Typewriter-Regular.woff2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      await cache.addAll(CORE_SHELL);
      await Promise.allSettled(OPTIONAL_SHELL.map((url) => cache.add(url)));
      await self.skipWaiting();
    })
  );
});

self.addEventListener("activate", (event) => {
  const OLD_PREFIXES = ["deepseek-mobile-", "deepseek-infra-"];
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) =>
              OLD_PREFIXES.some((pfx) => key.startsWith(pfx) && key !== CACHE_NAME)
            )
            .map((key) => caches.delete(key))
        )
      )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(event.request));
    return;
  }
  event.respondWith(staleWhileRevalidate(event));
});

async function networkFirstNavigation(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(canonicalCacheKey(request), response.clone());
    }
    return response;
  } catch {
    return (await caches.match(canonicalCacheKey(request))) || (await caches.match("/index.html"));
  }
}

async function staleWhileRevalidate(event) {
  const cache = await caches.open(CACHE_NAME);
  const key = canonicalCacheKey(event.request);
  const cached = await cache.match(key);
  const refresh = fetch(event.request).then(async (response) => {
    if (response.ok) await cache.put(key, response.clone());
    return response;
  });
  if (cached) {
    event.waitUntil(refresh.catch(() => undefined));
    return cached;
  }
  return refresh.catch(() => caches.match(key));
}

function canonicalCacheKey(request) {
  const url = new URL(typeof request === "string" ? request : request.url, self.location.origin);
  url.search = "";
  url.hash = "";
  return url.toString();
}

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type !== "show_reminder") return;
  const title = data.title || "DeepSeek 提醒";
  const options = {
    body: data.body || "",
    tag: data.tag || "deepseek-reminder",
    icon: "/icons/pwa-192x192.png",
    badge: "/icons/badge-96x96.png",
    data: { url: "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow("/");
    })
  );
});


