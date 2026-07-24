import { describe, expect, it, vi } from "vitest";

import { createBuildUpdateDriver, startWorkspaceServiceWorkerRuntime } from "./serviceWorkerRegistration";
import type { DeployedBuild } from "./buildUpdateStore";

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
    register: vi.fn(() => Promise.resolve({})),
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
      version: "4.3.3",
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

    const stop = await startWorkspaceServiceWorkerRuntime({
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
    stop();
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
    const stop = await pending;
    expect(serviceWorkers.register).toHaveBeenCalledWith(`/ui/sw-${BUILD_B}.js`, {
      scope: "/ui/",
      updateViaCache: "none",
    });
    expect(windowValue.requestIdleCallback).not.toHaveBeenCalled();
    stop();
  });

  it("reports the old page build even when a newer worker controls it", async () => {
    channels.length = 0;
    const workerB = controller(BUILD_B, DIGEST_B);
    const serviceWorkers = container(workerB);
    const stop = await startWorkspaceServiceWorkerRuntime({
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
    stop();
  });
});
