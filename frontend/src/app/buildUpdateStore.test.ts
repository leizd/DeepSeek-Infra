import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BuildUpdateStore,
  parseDeployedBuild,
  type BuildUpdateEnvironment,
  type BuildUpdateMessage,
  type DeployedBuild,
} from "./buildUpdateStore";
import {
  clearReloadBlocker,
  registerReloadFlusher,
  resetReloadCoordinationForTests,
  setReloadBlocker,
} from "./reloadBlockers";
import type { WorkerBuildIdentity } from "./workspaceOfflineWarmup";

const BUILD_A = "aaaaaaaaaaaaaaaa";
const BUILD_B = "bbbbbbbbbbbbbbbb";
const BUILD_C = "cccccccccccccccc";

function deployedBuild(buildId = BUILD_B): DeployedBuild {
  return {
    schemaVersion: 1,
    version: buildId === BUILD_C ? "4.3.4" : "4.3.3",
    sourceRevision: `revision-${buildId.slice(0, 8)}`,
    buildId,
    assetSetDigest: buildId[0].repeat(64),
  };
}

function identity(build: DeployedBuild): WorkerBuildIdentity {
  return {
    type: "build_identity",
    buildId: build.buildId,
    assetSetDigest: build.assetSetDigest,
    cacheReady: true,
  };
}

class FakeEventTarget {
  readonly listeners = new Map<string, Set<EventListener>>();

  addEventListener = vi.fn((type: string, listener: EventListener) => {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  });

  removeEventListener = vi.fn((type: string, listener: EventListener) => {
    this.listeners.get(type)?.delete(listener);
  });

  dispatch(type: string, event: Event = new Event(type)): void {
    this.listeners.get(type)?.forEach((listener) => listener(event));
  }
}

class FakeBroadcastChannel extends FakeEventTarget {
  readonly messages: BuildUpdateMessage[] = [];
  close = vi.fn();

  postMessage(message: BuildUpdateMessage): void {
    this.messages.push(message);
  }

  receive(message: BuildUpdateMessage): void {
    this.dispatch("message", { data: message } as MessageEvent);
  }
}

function environment(fetchValue: typeof fetch) {
  const windowTarget = new FakeEventTarget();
  const documentTarget = new FakeEventTarget();
  const channel = new FakeBroadcastChannel();
  let interval: (() => void) | undefined;
  const windowValue = {
    ...windowTarget,
    setInterval: vi.fn((callback: () => void) => {
      interval = callback;
      return 1;
    }),
    clearInterval: vi.fn(),
  };
  const documentValue = {
    ...documentTarget,
    visibilityState: "visible" as DocumentVisibilityState,
  };
  return {
    value: {
      fetchValue,
      windowValue,
      documentValue,
      createBroadcastChannel: () => channel,
    } as unknown as BuildUpdateEnvironment,
    windowTarget,
    documentTarget,
    documentValue,
    channel,
    runInterval: () => interval?.(),
  };
}

function response(build: DeployedBuild): Response {
  return new Response(JSON.stringify(build), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  resetReloadCoordinationForTests();
});

describe("deployed build validation", () => {
  it("accepts the signed build shape and rejects malformed identities", () => {
    const valid = deployedBuild();
    expect(parseDeployedBuild(valid)).toEqual(valid);
    expect(parseDeployedBuild({ ...valid, schemaVersion: 2 })).toBeNull();
    expect(parseDeployedBuild({ ...valid, buildId: "../worker" })).toBeNull();
    expect(parseDeployedBuild({ ...valid, assetSetDigest: "short" })).toBeNull();
    expect(parseDeployedBuild({ ...valid, sourceRevision: "bad revision" })).toBeNull();
  });
});

describe("build update discovery", () => {
  it("deduplicates concurrent checks, uses no-store with a signal, and stays current on failure", async () => {
    let resolveFetch!: (value: Response) => void;
    const fetchValue = vi.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.cache).toBe("no-store");
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      return new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      });
    }) as unknown as typeof fetch;
    const runtime = environment(fetchValue);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(),
      activate: vi.fn(),
      reload: vi.fn(),
    });

    const first = store.checkForUpdate();
    const second = store.checkForUpdate();
    expect(fetchValue).toHaveBeenCalledTimes(1);
    resolveFetch(new Response("unavailable", { status: 503 }));
    await Promise.all([first, second]);
    expect(store.getSnapshot().phase).toBe("current");
    stop();
  });

  it("checks at startup, online, visible, interval, and manual triggers", async () => {
    const fetchValue = vi.fn(() => Promise.resolve(response(deployedBuild(BUILD_A)))) as unknown as typeof fetch;
    const runtime = environment(fetchValue);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(),
      activate: vi.fn(),
      reload: vi.fn(),
    });
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("current"));

    runtime.windowTarget.dispatch("online");
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("current"));
    runtime.documentTarget.dispatch("visibilitychange");
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(3));
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("current"));
    runtime.runInterval();
    await vi.waitFor(() => expect(fetchValue).toHaveBeenCalledTimes(4));
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("current"));
    await store.checkForUpdate();
    expect(fetchValue).toHaveBeenCalledTimes(5);
    stop();
  });

  it("prevents a delayed B staging result from overwriting newer build C", async () => {
    let current = deployedBuild(BUILD_B);
    let resolveBuildB!: (value: WorkerBuildIdentity) => void;
    const stage = vi.fn((build: DeployedBuild) => {
      if (build.buildId === BUILD_C) return Promise.resolve(identity(build));
      return new Promise<WorkerBuildIdentity>((resolve) => {
        resolveBuildB = resolve;
      });
    });
    const fetchValue = vi.fn(() => Promise.resolve(response(current))) as unknown as typeof fetch;
    const runtime = environment(fetchValue);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage,
      activate: vi.fn(),
      reload: vi.fn(),
    });
    await vi.waitFor(() => expect(store.getSnapshot().targetBuildId).toBe(BUILD_B));

    current = deployedBuild(BUILD_C);
    await store.checkForUpdate();
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));
    expect(store.getSnapshot().targetBuildId).toBe(BUILD_C);

    resolveBuildB(identity(deployedBuild(BUILD_B)));
    await Promise.resolve();
    expect(store.getSnapshot()).toMatchObject({ phase: "ready", targetBuildId: BUILD_C });
    stop();
  });
});

describe("quiescent activation", () => {
  it("waits for blockers, revalidates the target, and reloads only after verified activation", async () => {
    const target = deployedBuild(BUILD_B);
    const fetchMock = vi.fn(() => Promise.resolve(response(target)));
    const fetchValue = fetchMock as unknown as typeof fetch;
    const activate = vi.fn(() => Promise.resolve(identity(target)));
    const reload = vi.fn();
    const runtime = environment(fetchValue);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate,
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    setReloadBlocker({
      id: "stream",
      label: "streaming",
      kind: "transient",
      active: true,
    });
    await store.activateWhenReady();
    expect(store.getSnapshot().phase).toBe("blocked");
    expect(activate).not.toHaveBeenCalled();

    clearReloadBlocker("stream");
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    expect(activate).toHaveBeenCalledWith(target);
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    stop();
  });

  it("marks another tab reload-required without forcing its reload", async () => {
    const target = deployedBuild(BUILD_B);
    const runtime = environment(
      vi.fn(() => Promise.resolve(response(target))) as unknown as typeof fetch,
    );
    const reload = vi.fn();
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate: vi.fn(),
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    runtime.channel.receive({ type: "build_activated", buildId: BUILD_B });
    expect(store.getSnapshot().phase).toBe("reload-required");
    expect(reload).not.toHaveBeenCalled();
    stop();
  });

  it("revalidates the stable pointer when another tab reports a staged build", async () => {
    const target = deployedBuild(BUILD_B);
    const fetchMock = vi.fn(() => Promise.resolve(response(deployedBuild(BUILD_A))));
    const runtime = environment(fetchMock as unknown as typeof fetch);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate: vi.fn(),
      reload: vi.fn(),
    });
    await store.checkForUpdate();
    expect(store.getSnapshot().phase).toBe("current");
    fetchMock.mockImplementation(() => Promise.resolve(response(target)));

    runtime.channel.receive({ type: "build_staged", buildId: BUILD_B });

    await vi.waitFor(() => expect(store.getSnapshot().targetBuildId).toBe(BUILD_B));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    stop();
  });

  it("discovers the target when the controller changes before the available broadcast arrives", async () => {
    const target = deployedBuild(BUILD_B);
    const fetchMock = vi.fn(() => Promise.resolve(response(deployedBuild(BUILD_A))));
    const runtime = environment(fetchMock as unknown as typeof fetch);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate: vi.fn(),
      reload: vi.fn(),
    });
    await store.checkForUpdate();
    fetchMock.mockImplementation(() => Promise.resolve(response(target)));

    store.noteControllerIdentity(identity(target));

    await vi.waitFor(() => expect(store.getSnapshot().targetBuildId).toBe(BUILD_B));
    expect(store.getSnapshot().controllerBuildId).toBe(BUILD_B);
    stop();
  });

  it("cancels activation before messaging the worker when synchronous persistence fails", async () => {
    const target = deployedBuild(BUILD_B);
    const runtime = environment(
      vi.fn(() => Promise.resolve(response(target))) as unknown as typeof fetch,
    );
    const activate = vi.fn();
    const reload = vi.fn();
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate,
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));
    const unregister = registerReloadFlusher("broken", () => {
      throw new Error("quota exceeded");
    });

    await store.activateWhenReady();

    expect(store.getSnapshot()).toMatchObject({
      phase: "error",
      error: "本地状态保存失败，已取消重新加载",
    });
    expect(activate).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
    unregister();
    stop();
  });
});

describe("activation transactions", () => {
  it("shares one single-flight activation across double clicks and auto unblock", async () => {
    const target = deployedBuild(BUILD_B);
    const runtime = environment(
      vi.fn(() => Promise.resolve(response(target))) as unknown as typeof fetch,
    );
    let resolveActivation!: (value: WorkerBuildIdentity) => void;
    const activate = vi.fn(
      () => new Promise<WorkerBuildIdentity>((resolve) => {
        resolveActivation = resolve;
      }),
    );
    const reload = vi.fn();
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate,
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    const first = store.activateWhenReady();
    const second = store.activateWhenReady();
    expect(second).toBe(first);
    await vi.waitFor(() => expect(activate).toHaveBeenCalledTimes(1));

    // A blocked auto-activation retry joins the same in-flight transaction.
    setReloadBlocker({ id: "stream", label: "streaming", kind: "transient", active: true });
    clearReloadBlocker("stream");
    expect(activate).toHaveBeenCalledTimes(1);

    resolveActivation(identity(target));
    await first;
    await second;
    expect(activate).toHaveBeenCalledTimes(1);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(runtime.channel.messages.filter((m) => m.type === "build_activated")).toHaveLength(1);
    stop();
  });

  it("discards a late B activation result after the target moved to C", async () => {
    let current = deployedBuild(BUILD_B);
    const fetchValue = vi.fn(() => Promise.resolve(response(current))) as unknown as typeof fetch;
    let resolveActivationB!: (value: WorkerBuildIdentity) => void;
    const activate = vi.fn((build: DeployedBuild) => {
      if (build.buildId === BUILD_C) return Promise.resolve(identity(build));
      return new Promise<WorkerBuildIdentity>((resolve) => {
        resolveActivationB = resolve;
      });
    });
    const reload = vi.fn();
    const runtime = environment(fetchValue);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn((build: DeployedBuild) => Promise.resolve(identity(build))),
      activate,
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    const activation = store.activateWhenReady();
    await vi.waitFor(() => expect(activate).toHaveBeenCalledTimes(1));

    current = deployedBuild(BUILD_C);
    await store.checkForUpdate();
    await vi.waitFor(() => expect(store.getSnapshot().targetBuildId).toBe(BUILD_C));
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    resolveActivationB(identity(deployedBuild(BUILD_B)));
    await activation;
    expect(reload).not.toHaveBeenCalled();
    expect(store.getSnapshot()).toMatchObject({ phase: "ready", targetBuildId: BUILD_C });
    expect(runtime.channel.messages.filter((m) => m.type === "build_activated")).toHaveLength(0);
    stop();
  });

  it("starts a new activation generation after an activation error", async () => {
    const target = deployedBuild(BUILD_B);
    const runtime = environment(
      vi.fn(() => Promise.resolve(response(target))) as unknown as typeof fetch,
    );
    const activate = vi.fn()
      .mockRejectedValueOnce(new Error("worker busy"))
      .mockImplementation(() => Promise.resolve(identity(target)));
    const reload = vi.fn();
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate,
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    await store.activateWhenReady();
    expect(store.getSnapshot().phase).toBe("error");
    expect(reload).not.toHaveBeenCalled();

    await store.activateWhenReady();
    expect(activate).toHaveBeenCalledTimes(2);
    expect(reload).toHaveBeenCalledTimes(1);
    stop();
  });
});

describe("phase-safe deferral", () => {
  it("defers while ready and cancels a transaction still revalidating", async () => {
    const target = deployedBuild(BUILD_B);
    let resolveRevalidation!: (value: Response) => void;
    const fetchMock = vi.fn()
      .mockImplementation(() => Promise.resolve(response(target)));
    const runtime = environment(fetchMock as unknown as typeof fetch);
    const activate = vi.fn(() => Promise.resolve(identity(target)));
    const reload = vi.fn();
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate,
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    store.defer();
    expect(store.getSnapshot().deferred).toBe(true);

    // 激活卡在复检阶段时，“稍后”取消整个事务。
    fetchMock.mockImplementation(
      () => new Promise<Response>((resolve) => {
        resolveRevalidation = resolve;
      }),
    );
    const activation = store.activateWhenReady();
    store.defer();
    expect(store.getSnapshot().deferred).toBe(true);

    resolveRevalidation(response(target));
    await activation;
    expect(activate).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
    expect(store.getSnapshot()).toMatchObject({ phase: "ready", deferred: true });
    stop();
  });

  it("cannot defer once activate_build has been requested", async () => {
    const target = deployedBuild(BUILD_B);
    const runtime = environment(
      vi.fn(() => Promise.resolve(response(target))) as unknown as typeof fetch,
    );
    let resolveActivation!: (value: WorkerBuildIdentity) => void;
    const activate = vi.fn(
      () => new Promise<WorkerBuildIdentity>((resolve) => {
        resolveActivation = resolve;
      }),
    );
    const reload = vi.fn();
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate,
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    const activation = store.activateWhenReady();
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("activating"));

    store.defer();
    expect(store.getSnapshot().deferred).toBe(false);

    resolveActivation(identity(target));
    await activation;
    expect(reload).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot().deferred).toBe(false);
    stop();
  });

  it("allows deferring a reload-required build", async () => {
    const target = deployedBuild(BUILD_B);
    const runtime = environment(
      vi.fn(() => Promise.resolve(response(target))) as unknown as typeof fetch,
    );
    let resolveActivation!: (value: WorkerBuildIdentity) => void;
    const activate = vi.fn(
      () => new Promise<WorkerBuildIdentity>((resolve) => {
        resolveActivation = resolve;
      }),
    );
    const reload = vi.fn();
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate,
      reload,
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    const activation = store.activateWhenReady();
    await vi.waitFor(() => expect(activate).toHaveBeenCalledTimes(1));
    setReloadBlocker({ id: "stream", label: "streaming", kind: "transient", active: true });
    resolveActivation(identity(target));
    await activation;
    expect(store.getSnapshot().phase).toBe("reload-required");
    expect(reload).not.toHaveBeenCalled();

    store.defer();
    expect(store.getSnapshot().deferred).toBe(true);

    clearReloadBlocker("stream");
    await Promise.resolve();
    expect(reload).not.toHaveBeenCalled();
    stop();
  });
});

describe("bounded update checks", () => {
  function abortableHangFetch(): typeof fetch {
    return vi.fn((_url: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    })) as unknown as typeof fetch;
  }

  it("releases a hung check after the timeout so later checks can proceed", async () => {
    const hanging = abortableHangFetch();
    const runtime = environment(hanging);
    (runtime.value as { checkTimeoutMs?: number }).checkTimeoutMs = 30;
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(),
      activate: vi.fn(),
      reload: vi.fn(),
    });
    expect(hanging).toHaveBeenCalledTimes(1);

    const timedOut = await store.checkForUpdate();
    expect(timedOut).toBeNull();
    expect(store.getSnapshot().phase).toBe("current");

    const target = deployedBuild(BUILD_B);
    const healthy = vi.fn(() => Promise.resolve(response(target))) as unknown as typeof fetch;
    (runtime.value as { fetchValue: typeof fetch }).fetchValue = healthy;
    const found = await store.checkForUpdate();
    expect(found?.buildId).toBe(BUILD_B);
    expect(healthy).toHaveBeenCalledTimes(1);
    stop();
  });

  it("lets a manual check supersede a hung request without the old finally clearing the new one", async () => {
    const hanging = vi.fn(() => new Promise<Response>(() => undefined)) as unknown as typeof fetch;
    const runtime = environment(hanging);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(deployedBuild(BUILD_B)))),
      activate: vi.fn(),
      reload: vi.fn(),
    });
    await Promise.resolve();
    expect(hanging).toHaveBeenCalledTimes(1);

    const target = deployedBuild(BUILD_B);
    const healthy = vi.fn(() => Promise.resolve(response(target))) as unknown as typeof fetch;
    (runtime.value as { fetchValue: typeof fetch }).fetchValue = healthy;
    const found = await store.checkForUpdate({ reason: "manual", force: true });
    expect(found?.buildId).toBe(BUILD_B);
    expect(healthy).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    // 旧请求的 finally 不得清理新请求状态：随后的检查照常发起。
    await store.checkForUpdate();
    expect(healthy).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot().targetBuildId).toBe(BUILD_B);
    stop();
  });

  it("replaces a check hung while offline when the tab comes back online", async () => {
    const hanging = vi.fn(() => new Promise<Response>(() => undefined)) as unknown as typeof fetch;
    const runtime = environment(hanging);
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(),
      activate: vi.fn(),
      reload: vi.fn(),
    });
    await Promise.resolve();
    expect(hanging).toHaveBeenCalledTimes(1);

    const healthy = vi.fn(() => Promise.resolve(response(deployedBuild(BUILD_A)))) as unknown as typeof fetch;
    (runtime.value as { fetchValue: typeof fetch }).fetchValue = healthy;
    runtime.windowTarget.dispatch("online");
    await vi.waitFor(() => expect(healthy).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("current"));
    stop();
  });

  it("keeps a confirmed ready target when a later check fails or times out", async () => {
    const target = deployedBuild(BUILD_B);
    const fetchMock = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(response(target)),
    );
    const runtime = environment(fetchMock as unknown as typeof fetch);
    (runtime.value as { checkTimeoutMs?: number }).checkTimeoutMs = 30;
    const store = new BuildUpdateStore(BUILD_A);
    const stop = store.configure(runtime.value, {
      stage: vi.fn(() => Promise.resolve(identity(target))),
      activate: vi.fn(),
      reload: vi.fn(),
    });
    await vi.waitFor(() => expect(store.getSnapshot().phase).toBe("ready"));

    fetchMock.mockImplementation(() => Promise.resolve(new Response("down", { status: 503 })));
    const failed = await store.checkForUpdate({ reason: "manual", force: true });
    expect(failed).toBeNull();
    expect(store.getSnapshot()).toMatchObject({ phase: "ready", targetBuildId: BUILD_B });

    fetchMock.mockImplementation(
      (_url: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }),
    );
    const timedOut = await store.checkForUpdate({ reason: "manual", force: true });
    expect(timedOut).toBeNull();
    expect(store.getSnapshot()).toMatchObject({ phase: "ready", targetBuildId: BUILD_B });
    stop();
  });
});
