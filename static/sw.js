const CACHE_PREFIX = "deepseek-mobile-";
const CACHE_NAME = "deepseek-mobile-v186";
const APP_SHELL = [
  "/",
  "/index.html",
  "/styles.css",
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
  "/vendor/katex/katex.min.css",
  "/vendor/katex/katex.min.js",
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
  "/math_core.js",
  "/seek_core.js",
  "/modules/network.js",
  "/modules/charts.js",
  "/modules/format.js",
  "/modules/markdown.js",
  "/modules/normalize.js",
  "/modules/settings.js",
  "/modules/panels.js",
  "/modules/reminder_parse.js",
  "/modules/speech_text.js",
  "/modules/stream.js",
  "/modules/agent_timeline.js",
  "/modules/chat.js",
  "/app.js",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

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


