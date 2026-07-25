import { afterEach, describe, expect, it, vi } from "vitest";

import { createBuildUpdateDriver, startWorkspaceServiceWorkerRuntime } from "./serviceWorkerRegistration";
import { BuildUpdateStore, type DeployedBuild } from "./buildUpdateStore";
import {
  getPersistenceHealthSnapshot,
  resetPersistenceHealthForTests,
} from "./persistenceHealth";
import {
  registerReloadFlusher,
  resetReloadCoordinationForTests,
} from "./reloadBlockers";

const BUILD_A = "aaaaaaaaaaaaaaaa";
const BUILD_B = "bbbbbbbbbbbbbbbb";
const DIGEST_A = "a".repeat(64);
const DIGEST_B = "b".repeat(64);

interface FakeMessageChannel extends MessageChannel {
  respond(data: unknown): void;
}

function messageChannel(): FakeMessageChannel {
  const port1 = {
    onmessage: null as ((event: MessageEvent) => void) | null,
    close: vi.fn(),
  };
  return {
    port1,
    port2: {},
    respond(data: unknown) {
      port1.onmessage?.({ data } as MessageEvent);
    },
  } as unknown as FakeMessageChannel;
}

function runtimeWindow(pathname = "/") {
  let nextHandle = 1;
  const timeouts = new Map<number, () => void>();
  const intervals = new Map<number, () => void>();
  const listeners = new Map<string, Set<EventListener>>();
  let idleCallback: (() => void) | undefined;
  return {
    location: { pathname, reload: vi.fn() },
    requestIdleCallback: vi.fn((callback: () => void) => {
      idleCallback = callback;
      return nextHandle++;
    }),
    setTimeout: vi.fn((callback: () => void) => {
      const handle = nextHandle++;
      timeouts.set(handle, callback);
      return handle;
    }),
    clearTimeout: vi.fn((handle: number) => timeouts.delete(handle)),
    setInterval: vi.fn((callback: () => void) => {
      const handle = nextHandle++;
      intervals.set(handle, callback);
      return handle;
    }),
    clearInterval: vi.fn((handle: number) => intervals.delete(handle)),
    addEventListener: vi.fn((type: string, listener: EventListener) => {
      const registered = listeners.get(type) ?? new Set<EventListener>();
      registered.add(listener);
      listeners.set(type, registered);
    }),
    removeEventListener: vi.fn((type: string, listener: EventListener) => {
      listeners.get(type)?.delete(listener);
    }),
    dispatch(type: string, event: Event) {
      listeners.get(type)?.forEach((listener) => listener(event));
    },
    runTimeouts() {
      for (const callback of [...timeouts.values()]) callback();
    },
    runIdle() {
      idleCallback?.();
    },
  };
}

function runtimeDocument() {
  const listeners = new Map<string, EventListener>();
  return {
    visibilityState: "visible",
    addEventListener: vi.fn((type: string, listener: EventListener) => listeners.set(type, listener)),
    removeEventListener: vi.fn((type: string) => listeners.delete(type)),
  };
}

function controller(buildId: string, assetSetDigest: string, respond = true) {
  return {
    postMessage: vi.fn((message: unknown, transfer?: Transferable[]) => {
      const request = message as { type?: string };
      if (request.type !== "get_build_identity" || !respond) return;
      const channel = transfer?.[0] as unknown as FakeMessageChannel["port2"] | undefined;
      const current = channels.find((candidate) => candidate.port2 === channel);
      current?.respond({
        type: "build_identity",
        buildId,
        assetSetDigest,
        cacheReady: true,
      });
    }),
  };
}

const channels: FakeMessageChannel[] = [];

function container(initialController: ReturnType<typeof controller> | null) {
  const listeners = new Map<string, EventListener>();
  return {
    controller: initialController,
    register: vi.fn((_scriptURL: string) => Promise.resolve({})),
    addEventListener: vi.fn((type: string, listener: EventListener) => listeners.set(type, listener)),
    removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    dispatch(type: string, event: Event = new Event(type)) {
      listeners.get(type)?.(event);
    },
  };
}

function channelFactory(): MessageChannel {
  const channel = messageChannel();
  channels.push(channel);
  return channel;
}

describe("build-scoped service worker registration", () => {
  it("stages a matching waiting worker and activates it only through the exact build message", async () => {
    channels.length = 0;
    const workerA = {
      ...controller(BUILD_A, DIGEST_A),
      scriptURL: `https://example.test/sw-${BUILD_A}.js`,
    };
    const serviceWorkers = container(workerA);
    const workerB = {
      scriptURL: `https://example.test/sw-${BUILD_B}.js`,
      state: "installed",
      postMessage: vi.fn((message: unknown, transfer?: Transferable[]) => {
        const request = message as { type?: string };
        if (request.type === "get_build_identity") {
          const channel = transfer?.[0] as unknown as FakeMessageChannel["port2"] | undefined;
          channels.find((candidate) => candidate.port2 === channel)?.respond({
            type: "build_identity",
            buildId: BUILD_B,
            assetSetDigest: DIGEST_B,
            cacheReady: true,
          });
        }
        if (request.type === "activate_build") {
          serviceWorkers.controller = workerB;
          serviceWorkers.dispatch("controllerchange");
        }
      }),
    };
    serviceWorkers.register.mockResolvedValue({
      waiting: workerB,
      active: workerA,
    });
    const windowValue = runtimeWindow();
    const driver = createBuildUpdateDriver(
      serviceWorkers,
      windowValue,
      true,
      channelFactory,
    );
    const build: DeployedBuild = {
      schemaVersion: 1,
      version: "4.3.6",
      sourceRevision: "revision-b",
      buildId: BUILD_B,
      assetSetDigest: DIGEST_B,
    };

    await expect(driver.stage(build)).resolves.toMatchObject({
      buildId: BUILD_B,
      assetSetDigest: DIGEST_B,
      cacheReady: true,
    });
    await expect(driver.activate(build)).resolves.toMatchObject({ buildId: BUILD_B });
    expect(workerB.postMessage).toHaveBeenCalledWith({
      type: "activate_build",
      buildId: BUILD_B,
      assetSetDigest: DIGEST_B,
    });
    expect(serviceWorkers.register).toHaveBeenCalledWith(`/sw-${BUILD_B}.js`, {
      scope: "/",
      updateViaCache: "none",
    });
    expect(serviceWorkers.register).toHaveBeenCalledTimes(1);

    driver.reload();
    expect(windowValue.location.reload).toHaveBeenCalledTimes(1);
  });

  it("does not warm page B through worker A, then warms once after controllerchange to B", async () => {
    channels.length = 0;
    const workerA = controller(BUILD_A, DIGEST_A);
    const workerB = controller(BUILD_B, DIGEST_B);
    const serviceWorkers = container(workerA);
    const windowValue = runtimeWindow();

    const runtime = await startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue,
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_B,
      createMessageChannel: channelFactory,
    });
    expect(serviceWorkers.register).toHaveBeenCalledWith(`/sw-${BUILD_B}.js`, {
      scope: "/",
      updateViaCache: "none",
    });
    expect(workerA.postMessage).toHaveBeenCalledWith({
      type: "report_build_lease",
      buildId: BUILD_B,
    });
    expect(windowValue.requestIdleCallback).not.toHaveBeenCalled();

    serviceWorkers.controller = workerB;
    serviceWorkers.dispatch("controllerchange");
    await Promise.resolve();
    expect(windowValue.requestIdleCallback).toHaveBeenCalledTimes(1);
    windowValue.runIdle();
    expect(workerB.postMessage).toHaveBeenCalledWith({
      type: "cache_workspace_primary",
      buildId: BUILD_B,
      assetSetDigest: DIGEST_B,
    });

    serviceWorkers.dispatch("controllerchange");
    await Promise.resolve();
    expect(windowValue.requestIdleCallback).toHaveBeenCalledTimes(1);
    runtime.dispose();
  });

  it("never substitutes registration.active when the controller handshake times out", async () => {
    channels.length = 0;
    const silentWorker = controller(BUILD_B, DIGEST_B, false);
    const serviceWorkers = container(silentWorker);
    const windowValue = runtimeWindow("/ui/");
    const pending = startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue,
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_B,
      createMessageChannel: channelFactory,
      handshakeTimeoutMs: 10,
    });
    await Promise.resolve();
    windowValue.runTimeouts();
    const runtime = await pending;
    expect(serviceWorkers.register).toHaveBeenCalledWith(`/ui/sw-${BUILD_B}.js`, {
      scope: "/ui/",
      updateViaCache: "none",
    });
    expect(windowValue.requestIdleCallback).not.toHaveBeenCalled();
    runtime.dispose();
  });

  it("reports the old page build even when a newer worker controls it", async () => {
    channels.length = 0;
    const workerB = controller(BUILD_B, DIGEST_B);
    const serviceWorkers = container(workerB);
    const runtime = await startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue: runtimeWindow(),
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_A,
      createMessageChannel: channelFactory,
    });
    expect(workerB.postMessage).toHaveBeenCalledWith({
      type: "report_build_lease",
      buildId: BUILD_A,
    });
    expect(workerB.postMessage).not.toHaveBeenCalledWith(expect.objectContaining({
      type: "cache_workspace_primary",
    }));
    runtime.dispose();
  });
});

function deployedBuild(buildId: string, assetSetDigest: string): DeployedBuild {
  return {
    schemaVersion: 1,
    version: "4.3.6",
    sourceRevision: `revision-${buildId.slice(0, 8)}`,
    buildId,
    assetSetDigest,
  };
}

function jsonResponse(build: DeployedBuild): Response {
  return new Response(JSON.stringify(build), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function postedTypes(postMessage: ReturnType<typeof vi.fn>): unknown[] {
  return postMessage.mock.calls.map(([message]) => (message as { type?: string }).type);
}

function broadcastChannelFactory() {
  return vi.fn(() => ({
    postMessage: vi.fn(),
    close: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
}

const PERSISTED_PAGESHOW = { persisted: true } as unknown as Event;

describe("bfcache runtime resync", () => {
  afterEach(() => {
    resetReloadCoordinationForTests();
    resetPersistenceHealthForTests();
  });

  it("resyncs persistence, build check, handshake and lease after a persisted pageshow, without reloads or model fetches", async () => {
    channels.length = 0;
    const workerA = controller(BUILD_A, DIGEST_A);
    const serviceWorkers = container(workerA);
    const windowValue = runtimeWindow();
    const store = new BuildUpdateStore(BUILD_A);
    const checkForUpdate = vi.spyOn(store, "checkForUpdate");
    const noteControllerIdentity = vi.spyOn(store, "noteControllerIdentity");
    const fetchUrls: string[] = [];
    const fetchValue = vi.fn((url: RequestInfo | URL) => {
      fetchUrls.push(String(url));
      return Promise.resolve(jsonResponse(deployedBuild(BUILD_A, DIGEST_A)));
    }) as unknown as typeof fetch;
    const flusher = vi.fn(() => ({ ok: true as const, revision: "revision-1" }));
    registerReloadFlusher("bfcache-retry", flusher);

    const runtime = await startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue,
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_A,
      createMessageChannel: channelFactory,
      buildUpdates: store,
      fetchValue,
      createBroadcastChannel: broadcastChannelFactory(),
    });
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(1));

    windowValue.dispatch("pageshow", PERSISTED_PAGESHOW);

    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(
      postedTypes(workerA.postMessage).filter((type) => type === "report_build_lease"),
    ).toHaveLength(2));
    expect(checkForUpdate).toHaveBeenCalledTimes(2);
    expect(checkForUpdate).toHaveBeenCalledWith({ reason: "bfcache", force: true });
    expect(noteControllerIdentity).toHaveBeenCalledTimes(2);
    expect(flusher).toHaveBeenCalledTimes(1);
    expect(getPersistenceHealthSnapshot().healthy).toBe(true);
    expect(getPersistenceHealthSnapshot().lastSuccessRevision["bfcache-retry"]).toBe("revision-1");
    expect(windowValue.location.reload).not.toHaveBeenCalled();
    expect(fetchUrls).toEqual(["/ui/workspace-assets.json", "/ui/workspace-assets.json"]);
    // 心跳与更新检查两个 interval 保持原样，不因恢复而重建。
    expect(windowValue.setInterval).toHaveBeenCalledTimes(2);
    runtime.dispose();
  });

  it("re-reports the lease when the heartbeat deadline was exceeded without re-creating the interval", async () => {
    channels.length = 0;
    let now = 1_000_000;
    const workerA = controller(BUILD_A, DIGEST_A);
    const serviceWorkers = container(workerA);
    const windowValue = runtimeWindow();
    const runtime = await startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue,
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_A,
      createMessageChannel: channelFactory,
      nowValue: () => now,
    });
    expect(postedTypes(workerA.postMessage).filter((type) => type === "report_build_lease")).toHaveLength(1);
    expect(windowValue.setInterval).toHaveBeenCalledTimes(1);

    // 冻结超过两个心跳周期：心跳疑似死亡，但句柄仍在，绝不重建第二个。
    now += 2 * 60_000 + 1;
    windowValue.dispatch("pageshow", PERSISTED_PAGESHOW);

    await vi.waitFor(() => expect(
      postedTypes(workerA.postMessage).filter((type) => type === "report_build_lease"),
    ).toHaveLength(2));
    expect(windowValue.setInterval).toHaveBeenCalledTimes(1);
    expect(windowValue.clearInterval).not.toHaveBeenCalled();
    runtime.dispose();
  });

  it("joins rapid restores into one resync without duplicating timers, channels or listeners", async () => {
    channels.length = 0;
    const workerA = controller(BUILD_A, DIGEST_A);
    const serviceWorkers = container(workerA);
    const windowValue = runtimeWindow();
    const store = new BuildUpdateStore(BUILD_A);
    const checkForUpdate = vi.spyOn(store, "checkForUpdate");
    const fetchValue = vi.fn(
      () => Promise.resolve(jsonResponse(deployedBuild(BUILD_A, DIGEST_A))),
    ) as unknown as typeof fetch;
    const createBroadcastChannel = broadcastChannelFactory();
    const flusher = vi.fn();
    registerReloadFlusher("bfcache-single-flight", flusher);

    const runtime = await startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue,
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_A,
      createMessageChannel: channelFactory,
      buildUpdates: store,
      fetchValue,
      createBroadcastChannel,
    });
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(1));

    let resolveCheck!: (value: Response) => void;
    (fetchValue as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise<Response>((resolve) => {
        resolveCheck = resolve;
      }),
    );
    windowValue.dispatch("pageshow", PERSISTED_PAGESHOW);
    windowValue.dispatch("pageshow", PERSISTED_PAGESHOW);
    const joined = runtime.resyncAfterBfcache();
    expect(runtime.resyncAfterBfcache()).toBe(joined);

    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(2));
    // 第二次恢复没有发起新的检查、握手或持久化重试。
    expect(checkForUpdate.mock.calls.filter(([options]) => options?.reason === "bfcache")).toHaveLength(1);
    expect(postedTypes(workerA.postMessage).filter((type) => type === "get_build_identity")).toHaveLength(1);
    expect(flusher).toHaveBeenCalledTimes(1);

    resolveCheck(jsonResponse(deployedBuild(BUILD_A, DIGEST_A)));
    await joined;

    expect(checkForUpdate.mock.calls.filter(([options]) => options?.reason === "bfcache")).toHaveLength(1);
    expect(postedTypes(workerA.postMessage).filter((type) => type === "get_build_identity")).toHaveLength(2);
    expect(postedTypes(workerA.postMessage).filter((type) => type === "report_build_lease")).toHaveLength(2);
    expect(flusher).toHaveBeenCalledTimes(1);
    expect(windowValue.setInterval).toHaveBeenCalledTimes(2);
    expect(createBroadcastChannel).toHaveBeenCalledTimes(1);
    expect(
      windowValue.addEventListener.mock.calls.filter(([type]) => type === "pageshow"),
    ).toHaveLength(1);
    expect(windowValue.location.reload).not.toHaveBeenCalled();
    runtime.dispose();
  });

  it("ignores a non-persisted pageshow", async () => {
    channels.length = 0;
    const workerA = controller(BUILD_A, DIGEST_A);
    const serviceWorkers = container(workerA);
    const windowValue = runtimeWindow();
    const store = new BuildUpdateStore(BUILD_A);
    const checkForUpdate = vi.spyOn(store, "checkForUpdate");
    const fetchValue = vi.fn(
      () => Promise.resolve(jsonResponse(deployedBuild(BUILD_A, DIGEST_A))),
    ) as unknown as typeof fetch;
    const flusher = vi.fn();
    registerReloadFlusher("bfcache-normal-load", flusher);

    const runtime = await startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue,
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_A,
      createMessageChannel: channelFactory,
      buildUpdates: store,
      fetchValue,
      createBroadcastChannel: broadcastChannelFactory(),
    });
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(1));

    windowValue.dispatch("pageshow", { persisted: false } as unknown as Event);
    await Promise.resolve();

    expect(checkForUpdate).toHaveBeenCalledTimes(1);
    expect(flusher).not.toHaveBeenCalled();
    expect(postedTypes(workerA.postMessage).filter((type) => type === "get_build_identity")).toHaveLength(1);
    expect(postedTypes(workerA.postMessage).filter((type) => type === "report_build_lease")).toHaveLength(1);
    runtime.dispose();
  });

  it("keeps the remaining resync steps running when the handshake fails", async () => {
    channels.length = 0;
    const silentWorker = controller(BUILD_A, DIGEST_A, false);
    const serviceWorkers = container(silentWorker);
    const windowValue = runtimeWindow();
    const store = new BuildUpdateStore(BUILD_A);
    const checkForUpdate = vi.spyOn(store, "checkForUpdate");
    const noteControllerIdentity = vi.spyOn(store, "noteControllerIdentity");
    const fetchValue = vi.fn(
      () => Promise.resolve(jsonResponse(deployedBuild(BUILD_A, DIGEST_A))),
    ) as unknown as typeof fetch;
    const flusher = vi.fn(() => ({ ok: true as const, revision: "revision-2" }));
    registerReloadFlusher("bfcache-failing-handshake", flusher);

    const pending = startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue,
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_A,
      createMessageChannel: channelFactory,
      handshakeTimeoutMs: 10,
      buildUpdates: store,
      fetchValue,
      createBroadcastChannel: broadcastChannelFactory(),
    });
    await Promise.resolve();
    windowValue.runTimeouts();
    const runtime = await pending;
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(1));

    windowValue.dispatch("pageshow", PERSISTED_PAGESHOW);
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(2));
    // 等握手请求真正发出（超时已登记）后再触发超时。
    await vi.waitFor(() => expect(
      postedTypes(silentWorker.postMessage).filter((type) => type === "get_build_identity"),
    ).toHaveLength(2));
    windowValue.runTimeouts();

    await vi.waitFor(() => expect(
      postedTypes(silentWorker.postMessage).filter((type) => type === "report_build_lease"),
    ).toHaveLength(2));
    expect(flusher).toHaveBeenCalledTimes(1);
    expect(checkForUpdate).toHaveBeenCalledWith({ reason: "bfcache", force: true });
    expect(postedTypes(silentWorker.postMessage).filter((type) => type === "get_build_identity")).toHaveLength(2);
    expect(noteControllerIdentity).not.toHaveBeenCalled();
    expect(getPersistenceHealthSnapshot().lastSuccessRevision["bfcache-failing-handshake"]).toBe("revision-2");
    expect(windowValue.location.reload).not.toHaveBeenCalled();
    runtime.dispose();
  });

  it("stages a build discovered on restore without activating it or reloading", async () => {
    channels.length = 0;
    const workerA = controller(BUILD_A, DIGEST_A);
    const serviceWorkers = container(workerA);
    const workerB = {
      scriptURL: `https://example.test/sw-${BUILD_B}.js`,
      state: "installed",
      postMessage: vi.fn((message: unknown, transfer?: Transferable[]) => {
        const request = message as { type?: string };
        if (request.type !== "get_build_identity") return;
        const channel = transfer?.[0] as unknown as FakeMessageChannel["port2"] | undefined;
        channels.find((candidate) => candidate.port2 === channel)?.respond({
          type: "build_identity",
          buildId: BUILD_B,
          assetSetDigest: DIGEST_B,
          cacheReady: true,
        });
      }),
    };
    serviceWorkers.register.mockImplementation((scriptURL: string) => Promise.resolve(
      scriptURL.includes(BUILD_B) ? { waiting: workerB } : {},
    ));
    let current = deployedBuild(BUILD_A, DIGEST_A);
    const fetchValue = vi.fn(
      () => Promise.resolve(jsonResponse(current)),
    ) as unknown as typeof fetch;
    const windowValue = runtimeWindow();
    const store = new BuildUpdateStore(BUILD_A);

    const runtime = await startWorkspaceServiceWorkerRuntime({
      container: serviceWorkers,
      navigatorValue: {},
      windowValue,
      documentValue: runtimeDocument(),
      pageBuildId: BUILD_A,
      createMessageChannel: channelFactory,
      buildUpdates: store,
      fetchValue,
      createBroadcastChannel: broadcastChannelFactory(),
    });
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(1));

    current = deployedBuild(BUILD_B, DIGEST_B);
    windowValue.dispatch("pageshow", PERSISTED_PAGESHOW);

    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));
    expect(store.getSnapshot().targetBuildId).toBe(BUILD_B);
    const posted = [
      ...postedTypes(workerA.postMessage),
      ...postedTypes(workerB.postMessage),
    ];
    expect(posted).not.toContain("activate_build");
    expect(windowValue.location.reload).not.toHaveBeenCalled();
    runtime.dispose();
  });
});
