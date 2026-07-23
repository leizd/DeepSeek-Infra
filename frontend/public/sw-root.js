const CACHE_PREFIX = "deepseek-react-root-";
const BUILD_HISTORY_CACHE = "deepseek-workspace-root-build-history";
const RETIRED_CACHE_PREFIXES = ["deepseek-mobile-", "deepseek-infra-"];
const SHELL_URL = "/";
const WORKER_BUILD_ID = "__DEEPSEEK_WORKER_BUILD_ID__";
const WORKER_ASSET_SET_DIGEST = "__DEEPSEEK_WORKER_ASSET_SET_DIGEST__";
const ASSET_MANIFEST_URL = "__DEEPSEEK_WORKER_MANIFEST_URL__";
const METADATA_PATH_PREFIX = "/__deepseek_workspace_metadata__/";
const LEASE_TIMEOUT_MS = 10 * 60 * 1000;
const LEASE_RECONCILE_DELAY_MS = 1000;
const BUILD_ID_PATTERN = /^[0-9a-f]{16}$/;
const ASSET_DIGEST_PATTERN = /^[0-9a-f]{64}$/;

let manifestPromise;
const warmupTasks = new Map();
const pruningBuildIds = new Set();
let leaseMutationTask = Promise.resolve();

function loadAssetManifest() {
  if (!manifestPromise) {
    manifestPromise = fetch(ASSET_MANIFEST_URL, { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`workspace asset manifest returned ${response.status}`);
        return response.json();
      })
      .then((manifest) => {
        if (
          manifest?.schemaVersion !== 1 ||
          manifest.buildId !== WORKER_BUILD_ID ||
          manifest.assetSetDigest !== WORKER_ASSET_SET_DIGEST ||
          !ASSET_DIGEST_PATTERN.test(manifest.assetSetDigest)
        ) {
          throw new Error("workspace asset manifest identity mismatch");
        }
        return manifest;
      })
      .catch((error) => {
        manifestPromise = undefined;
        throw error;
      });
  }
  return manifestPromise;
}

function buildCacheName(buildId) {
  return `${CACHE_PREFIX}${buildId}`;
}

async function metadataJson(key, fallback) {
  const metadata = await caches.open(BUILD_HISTORY_CACHE);
  const response = await metadata.match(`${METADATA_PATH_PREFIX}${encodeURIComponent(key)}`);
  return response ? response.json().catch(() => fallback) : fallback;
}

async function putMetadataJson(key, value) {
  const metadata = await caches.open(BUILD_HISTORY_CACHE);
  await metadata.put(
    `${METADATA_PATH_PREFIX}${encodeURIComponent(key)}`,
    new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } }),
  );
}

async function buildHistory() {
  const history = await metadataJson("builds", []);
  return Array.isArray(history) ? history.filter((item) => BUILD_ID_PATTERN.test(item)) : [];
}

async function rememberBuild(buildId) {
  const history = await buildHistory();
  const retained = [buildId, ...history.filter((item) => item !== buildId)].slice(0, 32);
  await putMetadataJson("builds", retained);
}

async function clientLeases() {
  const leases = await metadataJson("leases", {});
  return leases && typeof leases === "object" && !Array.isArray(leases) ? leases : {};
}

async function recordClientLease(clientId, buildId) {
  if (!clientId || !BUILD_ID_PATTERN.test(buildId)) return;
  const leases = await clientLeases();
  leases[clientId] = { clientId, buildId, lastSeenAt: Date.now() };
  await putMetadataJson("leases", leases);
}

async function activeClientIds() {
  const matched = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  return new Set(matched.map((client) => client.id));
}

async function protectedBuildIds(persistLeases = false) {
  const history = await buildHistory();
  const previous = history.find((buildId) => buildId !== WORKER_BUILD_ID);
  const protectedIds = new Set([WORKER_BUILD_ID]);
  if (previous) protectedIds.add(previous);

  const activeIds = await activeClientIds();
  const leases = await clientLeases();
  const now = Date.now();
  const retainedLeases = {};
  for (const [clientId, lease] of Object.entries(leases)) {
    if (
      !lease ||
      !BUILD_ID_PATTERN.test(lease.buildId) ||
      typeof lease.lastSeenAt !== "number"
    ) {
      continue;
    }
    if (activeIds.has(clientId) || now - lease.lastSeenAt <= LEASE_TIMEOUT_MS) {
      protectedIds.add(lease.buildId);
      retainedLeases[clientId] = lease;
    }
  }
  if (persistLeases) await putMetadataJson("leases", retainedLeases);
  return protectedIds;
}

async function pruneBuildCaches() {
  const protectedIds = await protectedBuildIds(true);
  const keys = await caches.keys();
  const history = await buildHistory();
  await putMetadataJson("builds", history.filter((buildId) => protectedIds.has(buildId)));
  const obsolete = keys
    .filter((key) => key.startsWith(CACHE_PREFIX))
    .map((key) => key.slice(CACHE_PREFIX.length))
    .filter((buildId) => !protectedIds.has(buildId));
  obsolete.forEach((buildId) => pruningBuildIds.add(buildId));
  await Promise.all([
    ...obsolete.map((buildId) => caches.delete(buildCacheName(buildId))),
    ...keys
      .filter((key) => RETIRED_CACHE_PREFIXES.some((prefix) => key.startsWith(prefix)))
      .map((key) => caches.delete(key)),
  ]);
}

function reconcileClientLease(clientId, buildId) {
  leaseMutationTask = leaseMutationTask
    .catch(() => undefined)
    .then(() => recordClientLease(clientId, buildId))
    .then(() => pruneBuildCaches());
  return leaseMutationTask;
}

async function cacheCore() {
  const manifest = await loadAssetManifest();
  const cache = await caches.open(buildCacheName(WORKER_BUILD_ID));
  await cache.addAll([SHELL_URL, ASSET_MANIFEST_URL, ...manifest.core]);
  await rememberBuild(WORKER_BUILD_ID);
}

async function cacheMissingWithConcurrency(cache, urls, limit = 3) {
  let next = 0;
  let complete = true;
  async function worker() {
    while (next < urls.length) {
      const url = urls[next];
      next += 1;
      if (await cache.match(url)) continue;
      try {
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) {
          complete = false;
          continue;
        }
        await cache.put(url, response.clone());
      } catch {
        complete = false;
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, urls.length) }, () => worker()));
  return complete;
}

async function performWorkspacePrimaryWarmup() {
  const manifest = await loadAssetManifest();
  const markerKey = `warmup:${WORKER_BUILD_ID}`;
  const marker = await metadataJson(markerKey, {});
  if (
    marker?.buildId === WORKER_BUILD_ID &&
    marker.assetSetDigest === WORKER_ASSET_SET_DIGEST &&
    marker.offlinePrimaryComplete === true
  ) {
    return true;
  }
  const cache = await caches.open(buildCacheName(WORKER_BUILD_ID));
  const complete = await cacheMissingWithConcurrency(cache, manifest.offlinePrimary || []);
  if (complete) {
    await putMetadataJson(markerKey, {
      buildId: WORKER_BUILD_ID,
      assetSetDigest: WORKER_ASSET_SET_DIGEST,
      offlinePrimaryComplete: true,
      completedAt: new Date().toISOString(),
    });
  }
  return complete;
}

function cacheWorkspacePrimary() {
  const existing = warmupTasks.get(WORKER_BUILD_ID);
  if (existing) return existing;
  const task = performWorkspacePrimaryWarmup().finally(() => {
    warmupTasks.delete(WORKER_BUILD_ID);
  });
  warmupTasks.set(WORKER_BUILD_ID, task);
  return task;
}

self.addEventListener("install", (event) => {
  event.waitUntil(cacheCore().then(() => self.skipWaiting()));
});

async function activateWorker() {
  await loadAssetManifest();
  await rememberBuild(WORKER_BUILD_ID);
  await self.clients.claim();
  const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of clients) {
    client.postMessage({ type: "worker_activated", buildId: WORKER_BUILD_ID });
  }
  await new Promise((resolve) => setTimeout(resolve, LEASE_RECONCILE_DELAY_MS));
  await pruneBuildCaches();
}

self.addEventListener("activate", (event) => {
  event.waitUntil(activateWorker());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/ui/workspace-assets.json") return;
  if (url.pathname === "/legacy" || url.pathname.startsWith("/legacy/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith(networkFirst(event.request));
    return;
  }
  event.respondWith(cacheFirstByBuild(event.request));
});

async function retainedBuildIds() {
  return [...await protectedBuildIds()];
}

async function currentBuildCache() {
  return caches.open(buildCacheName(WORKER_BUILD_ID));
}

function isHashedAsset(pathname) {
  return /-[A-Za-z0-9_-]{8,}\.(?:css|js|mjs|png|svg|webp|woff2?)$/i.test(pathname);
}

async function matchRuntimeAsset(request) {
  const current = await currentBuildCache();
  const currentMatch = await current.match(request);
  if (currentMatch) return currentMatch;

  const url = new URL(request.url);
  if (!isHashedAsset(url.pathname)) return undefined;
  const availableCaches = new Set(await caches.keys());
  for (const buildId of await retainedBuildIds()) {
    if (buildId === WORKER_BUILD_ID || pruningBuildIds.has(buildId)) continue;
    const cacheName = buildCacheName(buildId);
    if (!availableCaches.has(cacheName)) continue;
    const previousMatch = await caches.match(request, { cacheName });
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

async function workerBuildIdentity() {
  const cache = await currentBuildCache();
  const cacheReady = Boolean(await cache.match(SHELL_URL)) && Boolean(await cache.match(ASSET_MANIFEST_URL));
  return {
    type: "build_identity",
    buildId: WORKER_BUILD_ID,
    assetSetDigest: WORKER_ASSET_SET_DIGEST,
    cacheReady,
  };
}

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "get_build_identity") {
    const response = workerBuildIdentity().then((identity) => event.ports?.[0]?.postMessage(identity));
    event.waitUntil(response);
    return;
  }
  if (data.type === "report_build_lease") {
    event.waitUntil(reconcileClientLease(event.source?.id, data.buildId));
    return;
  }
  if (
    data.type === "cache_workspace_primary" &&
    data.buildId === WORKER_BUILD_ID &&
    data.assetSetDigest === WORKER_ASSET_SET_DIGEST
  ) {
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
