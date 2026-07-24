import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi, type Mock } from "vitest";

type RequestLike = string | { url: string };
type FetchMock = Mock<(request: RequestLike) => Promise<Response>>;

const BUILD_A = "aaaaaaaaaaaaaaaa";
const BUILD_B = "bbbbbbbbbbbbbbbb";
const BUILD_C = "cccccccccccccccc";
const DIGEST_C = "c".repeat(64);
const MANIFEST_URL = `https://example.test/ui/workspace-assets-${BUILD_C}.json`;

function metadataPath(shell: string, key: string): string {
  const prefix = shell === "/" ? "/__deepseek_workspace_metadata__/" : "/ui/__deepseek_workspace_metadata__/";
  return `${prefix}${encodeURIComponent(key)}`;
}

class FakeCache {
  readonly entries = new Map<string, Response>();
  putHook: (() => Promise<void>) | null = null;

  key(request: RequestLike): string {
    const value = typeof request === "string" ? request : request.url;
    return new URL(value, "https://example.test/").href;
  }

  async match(request: RequestLike): Promise<Response | undefined> {
    return this.entries.get(this.key(request))?.clone();
  }

  async put(request: RequestLike, response: Response): Promise<void> {
    if (this.putHook) await this.putHook();
    this.entries.set(this.key(request), response.clone());
  }

  async add(request: RequestLike): Promise<void> {
    const response = await this.fetchMock(request);
    if (!response.ok) throw new Error(`request failed with ${response.status}`);
    await this.put(request, response);
  }

  async addAll(requests: RequestLike[]): Promise<void> {
    for (const request of requests) await this.add(request);
  }

  constructor(private readonly fetchMock: FetchMock) {}
}

class FakeCacheStorage {
  readonly stores = new Map<string, FakeCache>();
  keysHook: (() => Promise<void> | void) | null = null;

  constructor(private readonly fetchMock: FetchMock) {}

  async open(name: string): Promise<FakeCache> {
    let cache = this.stores.get(name);
    if (!cache) {
      cache = new FakeCache(this.fetchMock);
      this.stores.set(name, cache);
    }
    return cache;
  }

  async keys(): Promise<string[]> {
    const keys = [...this.stores.keys()];
    if (this.keysHook) {
      const hook = this.keysHook;
      this.keysHook = null;
      await hook();
    }
    return keys;
  }

  async match(
    request: RequestLike,
    options: { cacheName?: string } = {},
  ): Promise<Response | undefined> {
    if (options.cacheName) return this.stores.get(options.cacheName)?.match(request);
    for (const cache of this.stores.values()) {
      const response = await cache.match(request);
      if (response) return response;
    }
    return undefined;
  }

  async delete(name: string): Promise<boolean> {
    return this.stores.delete(name);
  }
}

interface WaitEvent {
  waitUntil(promise: Promise<unknown>): void;
}

interface WorkerHarness {
  caches: FakeCacheStorage;
  fetchMock: FetchMock;
  clients: Array<{ id: string; postMessage: ReturnType<typeof vi.fn> }>;
  skipWaiting: ReturnType<typeof vi.fn>;
  fetch(request: { method: string; mode: string; url: string }): Promise<Response>;
  lifecycle(name: "install" | "activate"): Promise<void>;
  message(data: unknown, sourceId?: string, ports?: Array<{ postMessage(message: unknown): void }>): Promise<void>;
}

function workspaceManifest(overrides: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    version: "4.3.4",
    sourceRevision: "test-revision",
    buildId: BUILD_C,
    assetSetDigest: DIGEST_C,
    core: ["/ui/assets/core-abcdefgh.js"],
    offlinePrimary: [
      "/ui/assets/ProjectsFeature-abcdefgh.js",
      "/ui/assets/SkillsFeature-abcdefgh.js",
    ],
    recovery: [],
    routeOptional: [],
    ...overrides,
  };
}

async function workerHarness(
  workerName: "sw.js" | "sw-root.js",
  fetchMock: FetchMock,
  registrationActive = false,
): Promise<WorkerHarness> {
  const source = readFileSync(
    fileURLToPath(new URL(`../../public/${workerName}`, import.meta.url)),
    "utf8",
  )
    .replaceAll("__DEEPSEEK_WORKER_BUILD_ID__", BUILD_C)
    .replaceAll("__DEEPSEEK_WORKER_ASSET_SET_DIGEST__", DIGEST_C)
    .replaceAll("__DEEPSEEK_WORKER_MANIFEST_URL__", `/ui/workspace-assets-${BUILD_C}.json`);
  const listeners = new Map<string, (event: never) => void>();
  const caches = new FakeCacheStorage(fetchMock);
  const clients: Array<{ id: string; postMessage: ReturnType<typeof vi.fn> }> = [];
  const skipWaiting = vi.fn(() => Promise.resolve());
  const self = {
    location: { origin: "https://example.test" },
    addEventListener: (name: string, listener: (event: never) => void) => listeners.set(name, listener),
    clients: {
      claim: vi.fn(() => Promise.resolve()),
      matchAll: vi.fn(() => Promise.resolve(clients)),
      openWindow: vi.fn(),
    },
    registration: {
      active: registrationActive ? {} : null,
      showNotification: vi.fn(),
    },
    skipWaiting,
  };
  runInNewContext(source, {
    Date,
    Error,
    JSON,
    Map,
    Object,
    Promise,
    RegExp,
    Response,
    Set,
    URL,
    caches,
    fetch: fetchMock,
    self,
    setTimeout: (callback: () => void) => {
      callback();
      return 1;
    },
  });

  const waitForEvent = async (name: string, event: Record<string, unknown>) => {
    const waits: Promise<unknown>[] = [];
    listeners.get(name)?.({
      ...event,
      waitUntil(promise: Promise<unknown>) {
        waits.push(promise);
      },
    } as never);
    await Promise.all(waits);
  };

  return {
    caches,
    fetchMock,
    clients,
    skipWaiting,
    async fetch(request) {
      let responsePromise: Promise<Response> | undefined;
      listeners.get("fetch")?.({
        request,
        respondWith(response: Promise<Response>) {
          responsePromise = response;
        },
      } as never);
      if (!responsePromise) throw new Error("Service Worker did not handle request");
      return responsePromise;
    },
    lifecycle(name) {
      return waitForEvent(name, {});
    },
    message(data, sourceId = "client-1", ports = []) {
      return waitForEvent("message", {
        data,
        source: { id: sourceId },
        ports,
      });
    },
  };
}

const WORKERS = [
  {
    name: "sw.js" as const,
    prefix: "deepseek-react-ui-",
    history: "deepseek-workspace-ui-build-history",
    shell: "/ui/",
  },
  {
    name: "sw-root.js" as const,
    prefix: "deepseek-react-root-",
    history: "deepseek-workspace-root-build-history",
    shell: "/",
  },
];

async function seedBuilds(
  harness: WorkerHarness,
  prefix: string,
  historyName: string,
  shell: string,
) {
  const history = await harness.caches.open(historyName);
  await history.put(metadataPath(shell, "builds"), new Response(JSON.stringify([BUILD_C, BUILD_A])));
  const current = await harness.caches.open(`${prefix}${BUILD_C}`);
  const previous = await harness.caches.open(`${prefix}${BUILD_A}`);
  await current.put(shell, new Response("shell-c"));
  await current.put(MANIFEST_URL, new Response("manifest-c"));
  await previous.put(shell, new Response("shell-a"));
  await previous.put(
    "https://example.test/ui/assets/LegacyChunk-abcdefgh.js",
    new Response("legacy-chunk"),
  );
  return { current, previous };
}

describe.each(WORKERS)("$name immutable runtime", ({ name, prefix, history, shell }) => {
  it("activates the first install immediately after the core cache is ready", async () => {
    const fetchMock = vi.fn((request: RequestLike) => {
      const url = new URL(typeof request === "string" ? request : request.url, "https://example.test");
      if (url.href === MANIFEST_URL) {
        return Promise.resolve(new Response(JSON.stringify(workspaceManifest())));
      }
      return Promise.resolve(new Response(url.pathname));
    });
    const harness = await workerHarness(name, fetchMock);

    await harness.lifecycle("install");

    expect(harness.skipWaiting).toHaveBeenCalledTimes(1);
  });

  it("keeps upgrades waiting until a matching ready build is explicitly activated", async () => {
    const fetchMock = vi.fn((request: RequestLike) => {
      const url = new URL(typeof request === "string" ? request : request.url, "https://example.test");
      if (url.href === MANIFEST_URL) {
        return Promise.resolve(new Response(JSON.stringify(workspaceManifest())));
      }
      return Promise.resolve(new Response(url.pathname));
    });
    const harness = await workerHarness(name, fetchMock, true);
    await harness.lifecycle("install");
    expect(harness.skipWaiting).not.toHaveBeenCalled();

    await harness.message({
      type: "activate_build",
      buildId: BUILD_B,
      assetSetDigest: DIGEST_C,
    });
    await harness.message({
      type: "activate_build",
      buildId: BUILD_C,
      assetSetDigest: "d".repeat(64),
    });
    expect(harness.skipWaiting).not.toHaveBeenCalled();

    const request = {
      type: "activate_build",
      buildId: BUILD_C,
      assetSetDigest: DIGEST_C,
    };
    await harness.message(request);
    await harness.message(request);
    expect(harness.skipWaiting).toHaveBeenCalledTimes(1);
  });

  it("returns the current shell offline while preserving exact leased hash chunks", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new TypeError("offline")));
    const harness = await workerHarness(name, fetchMock);
    await seedBuilds(harness, prefix, history, shell);

    const navigation = await harness.fetch({
      method: "GET",
      mode: "navigate",
      url: `https://example.test${shell}`,
    });
    expect(await navigation.text()).toBe("shell-c");

    const legacyChunk = await harness.fetch({
      method: "GET",
      mode: "cors",
      url: "https://example.test/ui/assets/LegacyChunk-abcdefgh.js",
    });
    expect(await legacyChunk.text()).toBe("legacy-chunk");
  });

  it("never falls through to previous metadata or query-insensitive matches", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new TypeError("offline")));
    const harness = await workerHarness(name, fetchMock);
    const { current } = await seedBuilds(harness, prefix, history, shell);
    current.entries.delete(MANIFEST_URL);

    const manifest = await harness.fetch({
      method: "GET",
      mode: "cors",
      url: MANIFEST_URL,
    });
    expect(manifest.type).toBe("error");

    const searchedChunk = await harness.fetch({
      method: "GET",
      mode: "cors",
      url: "https://example.test/ui/assets/LegacyChunk-abcdefgh.js?build=wrong",
    });
    expect(searchedChunk.type).toBe("error");
  });

  it("does not recreate a previous cache deleted after the cache-key snapshot", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new TypeError("offline")));
    const harness = await workerHarness(name, fetchMock);
    await seedBuilds(harness, prefix, history, shell);
    harness.caches.keysHook = () => {
      harness.caches.stores.delete(`${prefix}${BUILD_A}`);
    };

    const response = await harness.fetch({
      method: "GET",
      mode: "cors",
      url: "https://example.test/ui/assets/LegacyChunk-abcdefgh.js",
    });

    expect(response.type).toBe("error");
    expect(harness.caches.stores.has(`${prefix}${BUILD_A}`)).toBe(false);
  });

  it("waits for a successful navigation response to reach the current cache", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response("network-shell")));
    const harness = await workerHarness(name, fetchMock);
    const { current } = await seedBuilds(harness, prefix, history, shell);
    let releasePut!: () => void;
    const putPending = new Promise<void>((resolve) => {
      releasePut = resolve;
    });
    current.putHook = () => putPending;

    let settled = false;
    const responsePromise = harness.fetch({
      method: "GET",
      mode: "navigate",
      url: `https://example.test${shell}`,
    }).then((response) => {
      settled = true;
      return response;
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(settled).toBe(false);
    releasePut();
    expect(await (await responsePromise).text()).toBe("network-shell");
    expect(await (await current.match(shell))?.text()).toBe("network-shell");
  });

  it("rejects install when the immutable manifest identity does not match", async () => {
    const fetchMock = vi.fn((request: RequestLike) => {
      const url = typeof request === "string" ? request : request.url;
      if (new URL(url, "https://example.test").href === MANIFEST_URL) {
        return Promise.resolve(new Response(JSON.stringify(workspaceManifest({ buildId: BUILD_B }))));
      }
      return Promise.resolve(new Response("asset"));
    });
    const harness = await workerHarness(name, fetchMock);
    await expect(harness.lifecycle("install")).rejects.toThrow("identity mismatch");
  });

  it("clears a rejected manifest promise so the same worker lifecycle can recover", async () => {
    let manifestAttempts = 0;
    const fetchMock = vi.fn((request: RequestLike) => {
      const url = new URL(typeof request === "string" ? request : request.url, "https://example.test");
      if (url.href === MANIFEST_URL) {
        manifestAttempts += 1;
        if (manifestAttempts === 1) return Promise.reject(new TypeError("temporary manifest outage"));
        return Promise.resolve(new Response(JSON.stringify(workspaceManifest())));
      }
      return Promise.resolve(new Response("asset"));
    });
    const harness = await workerHarness(name, fetchMock);
    const message = {
      type: "cache_workspace_primary",
      buildId: BUILD_C,
      assetSetDigest: DIGEST_C,
    };
    await expect(harness.message(message)).rejects.toThrow("temporary manifest outage");
    await expect(harness.message(message)).resolves.toBeUndefined();
    expect(manifestAttempts).toBe(2);
  });

  it("deduplicates warmup, skips cached assets, and resumes only missing failures", async () => {
    let skillsFailures = 1;
    const fetchMock = vi.fn((request: RequestLike) => {
      const url = new URL(typeof request === "string" ? request : request.url, "https://example.test");
      if (url.href === MANIFEST_URL) {
        return Promise.resolve(new Response(JSON.stringify(workspaceManifest())));
      }
      if (url.pathname.includes("SkillsFeature") && skillsFailures > 0) {
        skillsFailures -= 1;
        return Promise.reject(new TypeError("temporary failure"));
      }
      return Promise.resolve(new Response(url.pathname));
    });
    const harness = await workerHarness(name, fetchMock);
    const message = {
      type: "cache_workspace_primary",
      buildId: BUILD_C,
      assetSetDigest: DIGEST_C,
    };
    await Promise.all([harness.message(message, "tab-a"), harness.message(message, "tab-b")]);

    const projectsRequests = fetchMock.mock.calls.filter(([request]) =>
      String(typeof request === "string" ? request : request.url).includes("ProjectsFeature")
    );
    const skillsRequests = fetchMock.mock.calls.filter(([request]) =>
      String(typeof request === "string" ? request : request.url).includes("SkillsFeature")
    );
    expect(projectsRequests).toHaveLength(1);
    expect(skillsRequests).toHaveLength(1);

    await harness.message(message, "tab-a");
    const laterProjects = fetchMock.mock.calls.filter(([request]) =>
      String(typeof request === "string" ? request : request.url).includes("ProjectsFeature")
    );
    const laterSkills = fetchMock.mock.calls.filter(([request]) =>
      String(typeof request === "string" ? request : request.url).includes("SkillsFeature")
    );
    expect(laterProjects).toHaveLength(1);
    expect(laterSkills).toHaveLength(2);

    const metadata = await harness.caches.open(history);
    const marker = await metadata.match(metadataPath(shell, `warmup:${BUILD_C}`));
    expect(await marker?.json()).toEqual(expect.objectContaining({
      buildId: BUILD_C,
      assetSetDigest: DIGEST_C,
      offlinePrimaryComplete: true,
    }));
  });

  it("invalidates a warmup completion marker bound to a different asset digest", async () => {
    const fetchMock = vi.fn((request: RequestLike) => {
      const url = new URL(typeof request === "string" ? request : request.url, "https://example.test");
      if (url.href === MANIFEST_URL) {
        return Promise.resolve(new Response(JSON.stringify(workspaceManifest())));
      }
      return Promise.resolve(new Response(url.pathname));
    });
    const harness = await workerHarness(name, fetchMock);
    const metadata = await harness.caches.open(history);
    await metadata.put(metadataPath(shell, `warmup:${BUILD_C}`), new Response(JSON.stringify({
      buildId: BUILD_C,
      assetSetDigest: "d".repeat(64),
      offlinePrimaryComplete: true,
    })));
    await harness.message({
      type: "cache_workspace_primary",
      buildId: BUILD_C,
      assetSetDigest: DIGEST_C,
    });
    const marker = await metadata.match(metadataPath(shell, `warmup:${BUILD_C}`));
    expect(await marker?.json()).toEqual(expect.objectContaining({
      assetSetDigest: DIGEST_C,
      offlinePrimaryComplete: true,
    }));
    expect(fetchMock.mock.calls.some(([request]) =>
      String(typeof request === "string" ? request : request.url).includes("ProjectsFeature")
    )).toBe(true);
  });

  it("retains active client leases across A-B-C and prunes A after close and expiry", async () => {
    const fetchMock = vi.fn((request: RequestLike) => {
      const url = new URL(typeof request === "string" ? request : request.url, "https://example.test");
      if (url.href === MANIFEST_URL) {
        return Promise.resolve(new Response(JSON.stringify(workspaceManifest())));
      }
      return Promise.resolve(new Response("asset"));
    });
    const harness = await workerHarness(name, fetchMock);
    const metadata = await harness.caches.open(history);
    await metadata.put(metadataPath(shell, "builds"), new Response(JSON.stringify([BUILD_C, BUILD_B, BUILD_A])));
    await metadata.put(metadataPath(shell, "leases"), new Response(JSON.stringify({
      "client-a": { clientId: "client-a", buildId: BUILD_A, lastSeenAt: 0 },
    })));
    await harness.caches.open(`${prefix}${BUILD_C}`);
    await harness.caches.open(`${prefix}${BUILD_B}`);
    await harness.caches.open(`${prefix}${BUILD_A}`);
    harness.clients.push({ id: "client-a", postMessage: vi.fn() });

    await harness.lifecycle("activate");
    expect(harness.caches.stores.has(`${prefix}${BUILD_A}`)).toBe(true);

    harness.clients.splice(0, 1, { id: "client-c", postMessage: vi.fn() });
    await harness.message({ type: "report_build_lease", buildId: BUILD_C }, "client-c");
    expect(harness.caches.stores.has(`${prefix}${BUILD_A}`)).toBe(false);
    expect(harness.caches.stores.has(`${prefix}${BUILD_B}`)).toBe(true);
    expect(harness.caches.stores.has(`${prefix}${BUILD_C}`)).toBe(true);
  });

  it("returns the embedded identity through the handshake port", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new TypeError("unused")));
    const harness = await workerHarness(name, fetchMock);
    const current = await harness.caches.open(`${prefix}${BUILD_C}`);
    await current.put(shell, new Response("shell"));
    await current.put(MANIFEST_URL, new Response("manifest"));
    const responses: unknown[] = [];
    await harness.message(
      { type: "get_build_identity" },
      "client-c",
      [{ postMessage: (message) => responses.push(message) }],
    );
    expect(responses).toEqual([{
      type: "build_identity",
      buildId: BUILD_C,
      assetSetDigest: DIGEST_C,
      cacheReady: true,
    }]);
  });
});
