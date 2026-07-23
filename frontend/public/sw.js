const CACHE_PREFIX = "deepseek-react-ui-";
const BUILD_HISTORY_CACHE = "deepseek-workspace-ui-build-history";
const SHELL_URL = "/ui/";
const ASSET_MANIFEST_URL = "/ui/workspace-assets.json";

let manifestPromise;

function loadAssetManifest() {
  if (!manifestPromise) {
    manifestPromise = fetch(ASSET_MANIFEST_URL, { cache: "no-store" }).then((response) => {
      if (!response.ok) throw new Error(`workspace asset manifest returned ${response.status}`);
      return response.json();
    });
  }
  return manifestPromise;
}

function buildCacheName(buildId) {
  return `${CACHE_PREFIX}${buildId}`;
}

async function rememberBuild(buildId) {
  const metadata = await caches.open(BUILD_HISTORY_CACHE);
  const previous = await metadata.match("builds");
  const history = previous ? await previous.json().catch(() => []) : [];
  const retained = [buildId, ...history.filter((item) => item !== buildId)].slice(0, 2);
  await metadata.put("builds", new Response(JSON.stringify(retained), { headers: { "content-type": "application/json" } }));
  const keys = await caches.keys();
  await Promise.all(
    keys
      .filter((key) => key.startsWith(CACHE_PREFIX) && !retained.some((item) => key === buildCacheName(item)))
      .map((key) => caches.delete(key)),
  );
}

async function cacheCore() {
  const manifest = await loadAssetManifest();
  const cache = await caches.open(buildCacheName(manifest.buildId));
  await cache.addAll([SHELL_URL, ASSET_MANIFEST_URL, ...manifest.core]);
}

async function cacheOptional() {
  const manifest = await loadAssetManifest();
  const cache = await caches.open(buildCacheName(manifest.buildId));
  await Promise.allSettled(manifest.optional.map((url) => cache.add(url)));
}

self.addEventListener("install", (event) => {
  event.waitUntil(cacheCore().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    loadAssetManifest()
      .then((manifest) => rememberBuild(manifest.buildId))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith(networkFirst(event.request));
    return;
  }
  event.respondWith(staleWhileRevalidate(event.request));
});

async function currentCache() {
  const manifest = await loadAssetManifest();
  return caches.open(buildCacheName(manifest.buildId));
}

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) (await currentCache()).put(SHELL_URL, response.clone());
    return response;
  } catch {
    return (await caches.match(SHELL_URL)) || Response.error();
  }
}

async function staleWhileRevalidate(request) {
  const cached = await caches.match(request, { ignoreSearch: true });
  const refresh = fetch(request).then(async (response) => {
    if (response.ok) await (await currentCache()).put(request, response.clone());
    return response;
  });
  if (cached) {
    refresh.catch(() => undefined);
    return cached;
  }
  return refresh.catch(() => caches.match(request, { ignoreSearch: true }));
}

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "cache_optional_workspace") {
    event.waitUntil(cacheOptional());
    return;
  }
  if (data.type !== "show_reminder") return;
  event.waitUntil(
    self.registration.showNotification(data.title || "DeepSeek 提醒", {
      body: data.body || "",
      tag: data.tag || "deepseek-reminder",
      icon: "/icons/pwa-192x192.png",
      badge: "/icons/badge-96x96.png",
      data: { url: "/ui/" },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow("/ui/");
    }),
  );
});
