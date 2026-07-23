const CACHE_PREFIX = "deepseek-react-root-";
const BUILD_HISTORY_CACHE = "deepseek-workspace-root-build-history";
const RETIRED_CACHE_PREFIXES = ["deepseek-mobile-", "deepseek-infra-"];
const SHELL_URL = "/";
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
      .filter(
        (key) =>
          (key.startsWith(CACHE_PREFIX) && !retained.some((item) => key === buildCacheName(item))) ||
          RETIRED_CACHE_PREFIXES.some((prefix) => key.startsWith(prefix)),
      )
      .map((key) => caches.delete(key)),
  );
}

async function cacheCore() {
  const manifest = await loadAssetManifest();
  const cache = await caches.open(buildCacheName(manifest.buildId));
  await cache.addAll([SHELL_URL, ASSET_MANIFEST_URL, ...manifest.core]);
}

async function cacheWithConcurrency(cache, urls, limit = 3) {
  let next = 0;
  async function worker() {
    while (next < urls.length) {
      const url = urls[next];
      next += 1;
      await cache.add(url).catch(() => undefined);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, urls.length) }, () => worker()));
}

async function cacheWorkspacePrimary() {
  const manifest = await loadAssetManifest();
  const cache = await caches.open(buildCacheName(manifest.buildId));
  await cacheWithConcurrency(cache, manifest.offlinePrimary || []);
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
  if (url.pathname === "/legacy" || url.pathname.startsWith("/legacy/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith(networkFirst(event.request));
    return;
  }
  event.respondWith(cacheFirstByBuild(event.request));
});

async function retainedBuildIds() {
  const metadata = await caches.open(BUILD_HISTORY_CACHE);
  const response = await metadata.match("builds");
  const history = response ? await response.json().catch(() => []) : [];
  return Array.isArray(history) ? history.filter((item) => typeof item === "string") : [];
}

async function currentBuildId() {
  try {
    return (await loadAssetManifest()).buildId;
  } catch {
    return (await retainedBuildIds())[0];
  }
}

async function currentBuildCache() {
  const buildId = await currentBuildId();
  if (!buildId) throw new Error("current Workspace build is unavailable");
  return caches.open(buildCacheName(buildId));
}

function isHashedAsset(pathname) {
  return /-[A-Za-z0-9_-]{8,}\.(?:css|js|mjs|png|svg|webp|woff2?)$/i.test(pathname);
}

async function matchRuntimeAsset(request) {
  const currentId = await currentBuildId();
  if (!currentId) return undefined;
  const current = await caches.open(buildCacheName(currentId));
  const currentMatch = await current.match(request);
  if (currentMatch) return currentMatch;

  const url = new URL(request.url);
  if (url.pathname === ASSET_MANIFEST_URL || !isHashedAsset(url.pathname)) return undefined;
  for (const buildId of await retainedBuildIds()) {
    if (buildId === currentId) continue;
    const previousMatch = await (await caches.open(buildCacheName(buildId))).match(request);
    if (previousMatch) return previousMatch;
  }
  return undefined;
}

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) await (await currentBuildCache()).put(SHELL_URL, response.clone());
    return response;
  } catch {
    return (await (await currentBuildCache()).match(SHELL_URL)) || Response.error();
  }
}

async function cacheFirstByBuild(request) {
  const cached = await matchRuntimeAsset(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) await (await currentBuildCache()).put(request, response.clone());
    return response;
  } catch {
    return Response.error();
  }
}

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "cache_workspace_primary") {
    event.waitUntil(cacheWorkspacePrimary());
    return;
  }
  if (data.type !== "show_reminder") return;
  event.waitUntil(
    self.registration.showNotification(data.title || "DeepSeek 提醒", {
      body: data.body || "",
      tag: data.tag || "deepseek-reminder",
      icon: "/icons/pwa-192x192.png",
      badge: "/icons/badge-96x96.png",
      data: { url: "/" },
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
      return self.clients.openWindow("/");
    }),
  );
});
