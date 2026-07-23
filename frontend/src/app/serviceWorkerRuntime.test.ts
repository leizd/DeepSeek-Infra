import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

type RequestLike = string | { url: string };

class FakeCache {
  readonly entries = new Map<string, Response>();
  putHook: (() => Promise<void>) | null = null;

  key(request: RequestLike): string {
    return typeof request === "string" ? request : request.url;
  }

  async match(request: RequestLike): Promise<Response | undefined> {
    return this.entries.get(this.key(request))?.clone();
  }

  async put(request: RequestLike, response: Response): Promise<void> {
    if (this.putHook) await this.putHook();
    this.entries.set(this.key(request), response.clone());
  }

  async add(): Promise<void> {
    throw new Error("not used by fetch policy tests");
  }

  async addAll(): Promise<void> {
    throw new Error("not used by fetch policy tests");
  }
}

class FakeCacheStorage {
  readonly stores = new Map<string, FakeCache>();

  async open(name: string): Promise<FakeCache> {
    let cache = this.stores.get(name);
    if (!cache) {
      cache = new FakeCache();
      this.stores.set(name, cache);
    }
    return cache;
  }

  async keys(): Promise<string[]> {
    return [...this.stores.keys()];
  }

  async delete(name: string): Promise<boolean> {
    return this.stores.delete(name);
  }
}

interface WorkerHarness {
  caches: FakeCacheStorage;
  fetchMock: ReturnType<typeof vi.fn>;
  fetch(request: { method: string; mode: string; url: string }): Promise<Response>;
}

async function workerHarness(
  workerName: "sw.js" | "sw-root.js",
  fetchMock: ReturnType<typeof vi.fn>,
): Promise<WorkerHarness> {
  const source = readFileSync(
    fileURLToPath(new URL(`../../public/${workerName}`, import.meta.url)),
    "utf8",
  );
  const listeners = new Map<string, (event: {
    request: { method: string; mode: string; url: string };
    respondWith(response: Promise<Response>): void;
  }) => void>();
  const caches = new FakeCacheStorage();
  const self = {
    location: { origin: "https://example.test" },
    addEventListener: (name: string, listener: never) => listeners.set(name, listener),
    clients: {},
    registration: {},
  };
  runInNewContext(source, {
    Array,
    Error,
    JSON,
    Map,
    Promise,
    RegExp,
    Response,
    Set,
    URL,
    caches,
    fetch: fetchMock,
    self,
  });
  return {
    caches,
    fetchMock,
    async fetch(request) {
      let responsePromise: Promise<Response> | undefined;
      listeners.get("fetch")?.({
        request,
        respondWith(response) {
          responsePromise = response;
        },
      });
      if (!responsePromise) throw new Error("Service Worker did not handle request");
      return responsePromise;
    },
  };
}

const WORKERS = [
  { name: "sw.js" as const, prefix: "deepseek-react-ui-", history: "deepseek-workspace-ui-build-history", shell: "/ui/" },
  { name: "sw-root.js" as const, prefix: "deepseek-react-root-", history: "deepseek-workspace-root-build-history", shell: "/" },
];

async function seedBuilds(harness: WorkerHarness, prefix: string, historyName: string, shell: string) {
  const history = await harness.caches.open(historyName);
  history.entries.set("builds", new Response(JSON.stringify(["build-b", "build-a"])));
  const current = await harness.caches.open(`${prefix}build-b`);
  const previous = await harness.caches.open(`${prefix}build-a`);
  current.entries.set(shell, new Response("shell-b"));
  current.entries.set("https://example.test/ui/workspace-assets.json", new Response("manifest-b"));
  previous.entries.set(shell, new Response("shell-a"));
  previous.entries.set("https://example.test/ui/workspace-assets.json", new Response("manifest-a"));
  previous.entries.set("https://example.test/ui/assets/LegacyChunk-abcdefgh.js", new Response("legacy-chunk"));
  return { current, previous };
}

describe.each(WORKERS)("$name cache ordering", ({ name, prefix, history, shell }) => {
  it("returns the current shell offline while preserving exact previous-build hash chunks", async () => {
    const fetchMock = vi.fn(() => Promise.reject(new TypeError("offline")));
    const harness = await workerHarness(name, fetchMock);
    await seedBuilds(harness, prefix, history, shell);

    const navigation = await harness.fetch({
      method: "GET",
      mode: "navigate",
      url: `https://example.test${shell}`,
    });
    expect(await navigation.text()).toBe("shell-b");

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
    current.entries.delete("https://example.test/ui/workspace-assets.json");

    const manifest = await harness.fetch({
      method: "GET",
      mode: "cors",
      url: "https://example.test/ui/workspace-assets.json",
    });
    expect(manifest.type).toBe("error");

    const searchedChunk = await harness.fetch({
      method: "GET",
      mode: "cors",
      url: "https://example.test/ui/assets/LegacyChunk-abcdefgh.js?build=wrong",
    });
    expect(searchedChunk.type).toBe("error");
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
});
