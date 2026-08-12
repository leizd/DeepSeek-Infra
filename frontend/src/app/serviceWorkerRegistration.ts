import {
  scheduleWorkspaceOfflineWarmup,
  type WorkerBuildIdentity,
} from "./workspaceOfflineWarmup";
import {
  type BuildUpdateEnvironment,
  type BuildUpdateStore,
  type DeployedBuild,
} from "./buildUpdateStore";
import { recordFlushReport } from "./persistenceHealth";
import { flushReloadPersistence } from "./reloadBlockers";

const BUILD_ID_PATTERN = /^[0-9a-f]{16}$/;
const ASSET_DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const HANDSHAKE_TIMEOUT_MS = 5000;
const LEASE_HEARTBEAT_MS = 60_000;
const LEASE_HEARTBEAT_DEAD_MS = 2 * LEASE_HEARTBEAT_MS;

interface WorkerControllerLike {
  postMessage(message: unknown, transfer?: Transferable[]): void;
}

interface ServiceWorkerLike extends WorkerControllerLike {
  scriptURL?: string;
  state?: string;
  addEventListener?(type: "statechange", listener: EventListener): void;
  removeEventListener?(type: "statechange", listener: EventListener): void;
}

interface ServiceWorkerRegistrationLike {
  installing?: ServiceWorkerLike | null;
  waiting?: ServiceWorkerLike | null;
  active?: ServiceWorkerLike | null;
  addEventListener?(type: "updatefound", listener: EventListener): void;
  removeEventListener?(type: "updatefound", listener: EventListener): void;
}

interface ServiceWorkerContainerLike {
  controller: ServiceWorkerLike | null;
  register(scriptURL: string, options: RegistrationOptions): Promise<ServiceWorkerRegistrationLike>;
  addEventListener(type: "controllerchange" | "message", listener: EventListener): void;
  removeEventListener(type: "controllerchange" | "message", listener: EventListener): void;
}

interface RuntimeWindowLike {
  location: { pathname: string; reload?: () => void };
  requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
  setTimeout(callback: () => void, timeout: number): number;
  clearTimeout(handle: number): void;
  setInterval(callback: () => void, timeout: number): number;
  clearInterval(handle: number): void;
  addEventListener(type: "pageshow", listener: EventListener): void;
  removeEventListener(type: "pageshow", listener: EventListener): void;
}

interface RuntimeDocumentLike {
  visibilityState: string;
  addEventListener(type: "visibilitychange", listener: EventListener): void;
  removeEventListener(type: "visibilitychange", listener: EventListener): void;
}

interface StartServiceWorkerOptions {
  container: ServiceWorkerContainerLike;
  navigatorValue: object;
  windowValue: RuntimeWindowLike;
  documentValue: RuntimeDocumentLike;
  pageBuildId: string;
  createMessageChannel?: () => MessageChannel;
  handshakeTimeoutMs?: number;
  buildUpdates?: BuildUpdateStore;
  fetchValue?: typeof fetch;
  createBroadcastChannel?: BuildUpdateEnvironment["createBroadcastChannel"];
  nowValue?: () => number;
}

export interface WorkspaceServiceWorkerRuntime {
  dispose(): void;
  resyncAfterBfcache(): Promise<void>;
}

function validWorkerIdentity(value: unknown): value is WorkerBuildIdentity {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<WorkerBuildIdentity>;
  return candidate.type === "build_identity"
    && typeof candidate.buildId === "string"
    && BUILD_ID_PATTERN.test(candidate.buildId)
    && typeof candidate.assetSetDigest === "string"
    && ASSET_DIGEST_PATTERN.test(candidate.assetSetDigest)
    && typeof candidate.cacheReady === "boolean";
}

export function requestWorkerBuildIdentity(
  controller: WorkerControllerLike,
  windowValue: Pick<RuntimeWindowLike, "setTimeout" | "clearTimeout">,
  createMessageChannel: () => MessageChannel = () => new MessageChannel(),
  timeoutMs = HANDSHAKE_TIMEOUT_MS,
): Promise<WorkerBuildIdentity | null> {
  return new Promise((resolve) => {
    const channel = createMessageChannel();
    let settled = false;
    const finish = (identity: WorkerBuildIdentity | null) => {
      if (settled) return;
      settled = true;
      windowValue.clearTimeout(timer);
      channel.port1.close();
      resolve(identity);
    };
    const timer = windowValue.setTimeout(() => finish(null), timeoutMs);
    channel.port1.onmessage = (event) => {
      finish(validWorkerIdentity(event.data) ? event.data : null);
    };
    controller.postMessage({ type: "get_build_identity" }, [channel.port2]);
  });
}

export function reportPageBuildLease(controller: WorkerControllerLike, pageBuildId: string): void {
  controller.postMessage({ type: "report_build_lease", buildId: pageBuildId });
}

function workerUrl(atRoot: boolean, buildId: string): string {
  if (!BUILD_ID_PATTERN.test(buildId)) throw new Error("Invalid worker build ID");
  return atRoot ? `/sw-${buildId}.js` : `/ui/sw-${buildId}.js`;
}

function workerMatchesBuild(worker: ServiceWorkerLike | null | undefined, buildId: string): boolean {
  if (!worker) return false;
  if (!worker.scriptURL) return true;
  return new URL(worker.scriptURL, "https://deepseek.invalid").pathname.endsWith(`/sw-${buildId}.js`);
}

function waitForStagedWorker(
  registration: ServiceWorkerRegistrationLike,
  buildId: string,
  windowValue: Pick<RuntimeWindowLike, "setTimeout" | "clearTimeout">,
  timeoutMs = HANDSHAKE_TIMEOUT_MS * 3,
): Promise<ServiceWorkerLike> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const watched = new Map<ServiceWorkerLike, EventListener>();
    const finish = (worker: ServiceWorkerLike | null | undefined, error?: Error) => {
      if (settled) return;
      if (error) {
        settled = true;
        cleanup();
        reject(error);
        return;
      }
      if (!worker || !workerMatchesBuild(worker, buildId)) return;
      settled = true;
      cleanup();
      resolve(worker);
    };
    const inspect = () => {
      if (registration.waiting && workerMatchesBuild(registration.waiting, buildId)) {
        finish(registration.waiting);
        return;
      }
      if (registration.active && workerMatchesBuild(registration.active, buildId)) {
        finish(registration.active);
        return;
      }
      const installing = registration.installing;
      if (!installing || !workerMatchesBuild(installing, buildId)) return;
      if (["installed", "activating", "activated"].includes(installing.state ?? "")) {
        finish(installing);
        return;
      }
      if (watched.has(installing)) return;
      const onStateChange: EventListener = () => {
        if (installing.state === "redundant") {
          finish(null, new Error("更新 Worker 安装失败"));
          return;
        }
        if (["installed", "activating", "activated"].includes(installing.state ?? "")) {
          finish(installing);
          return;
        }
        inspect();
      };
      watched.set(installing, onStateChange);
      installing.addEventListener?.("statechange", onStateChange);
    };
    const onUpdateFound: EventListener = () => inspect();
    const cleanup = () => {
      windowValue.clearTimeout(timer);
      registration.removeEventListener?.("updatefound", onUpdateFound);
      for (const [worker, listener] of watched) {
        worker.removeEventListener?.("statechange", listener);
      }
      watched.clear();
    };
    const timer = windowValue.setTimeout(
      () => finish(null, new Error("等待更新 Worker 超时")),
      timeoutMs,
    );
    registration.addEventListener?.("updatefound", onUpdateFound);
    inspect();
  });
}

function waitForControllerIdentity(
  container: ServiceWorkerContainerLike,
  build: DeployedBuild,
  windowValue: Pick<RuntimeWindowLike, "setTimeout" | "clearTimeout">,
  createMessageChannel?: () => MessageChannel,
  timeoutMs = HANDSHAKE_TIMEOUT_MS * 3,
): Promise<WorkerBuildIdentity> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (identity: WorkerBuildIdentity | null, error?: Error) => {
      if (settled) return;
      if (
        identity
        && identity.buildId === build.buildId
        && identity.assetSetDigest === build.assetSetDigest
        && identity.cacheReady
      ) {
        settled = true;
        cleanup();
        resolve(identity);
        return;
      }
      if (!error) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const inspect = async () => {
      const controller = container.controller;
      if (!controller) return;
      const identity = await requestWorkerBuildIdentity(
        controller,
        windowValue,
        createMessageChannel,
        Math.min(timeoutMs, HANDSHAKE_TIMEOUT_MS),
      );
      finish(identity);
    };
    const onControllerChange: EventListener = () => {
      void inspect();
    };
    const cleanup = () => {
      windowValue.clearTimeout(timer);
      container.removeEventListener("controllerchange", onControllerChange);
    };
    const timer = windowValue.setTimeout(
      () => finish(null, new Error("等待新 Worker 接管超时")),
      timeoutMs,
    );
    container.addEventListener("controllerchange", onControllerChange);
    void inspect();
  });
}

export function createBuildUpdateDriver(
  container: ServiceWorkerContainerLike,
  windowValue: RuntimeWindowLike,
  atRoot: boolean,
  createMessageChannel?: () => MessageChannel,
): {
  stage(build: DeployedBuild): Promise<WorkerBuildIdentity>;
  activate(build: DeployedBuild): Promise<WorkerBuildIdentity>;
  discard(build: DeployedBuild): Promise<void>;
  reload(): void;
} {
  const rootRegistrations = new Map<string, ServiceWorkerRegistrationLike>();
  const rootScope = atRoot ? "/" : "/ui/";

  const rootRegistrationFor = async (build: DeployedBuild): Promise<ServiceWorkerRegistrationLike> => {
    const existing = rootRegistrations.get(build.buildId);
    if (existing) return existing;
    const registration = await container.register(workerUrl(atRoot, build.buildId), {
      scope: rootScope,
      updateViaCache: "none",
    });
    rootRegistrations.set(build.buildId, registration);
    return registration;
  };

  return {
    async stage(build) {
      const registration = await rootRegistrationFor(build);
      const worker = await waitForStagedWorker(registration, build.buildId, windowValue);
      const identity = await requestWorkerBuildIdentity(worker, windowValue, createMessageChannel);
      if (
        !identity
        || identity.buildId !== build.buildId
        || identity.assetSetDigest !== build.assetSetDigest
        || !identity.cacheReady
      ) {
        throw new Error("等待中的 Worker 身份或 Core Cache 无效");
      }
      return identity;
    },
    async activate(build) {
      const controllerIdentity = container.controller
        ? await requestWorkerBuildIdentity(container.controller, windowValue, createMessageChannel)
        : null;
      if (
        controllerIdentity?.buildId === build.buildId
        && controllerIdentity.assetSetDigest === build.assetSetDigest
        && controllerIdentity.cacheReady
      ) {
        return controllerIdentity;
      }
      const registration = await rootRegistrationFor(build);
      const worker = await waitForStagedWorker(registration, build.buildId, windowValue);
      const waitingIdentity = await requestWorkerBuildIdentity(worker, windowValue, createMessageChannel);
      if (
        !waitingIdentity
        || waitingIdentity.buildId !== build.buildId
        || waitingIdentity.assetSetDigest !== build.assetSetDigest
        || !waitingIdentity.cacheReady
      ) {
        throw new Error("更新 Worker 尚未准备好");
      }
      const controllerPromise = waitForControllerIdentity(
        container,
        build,
        windowValue,
        createMessageChannel,
      );
      worker.postMessage({
        type: "activate_build",
        buildId: build.buildId,
        assetSetDigest: build.assetSetDigest,
      });
      const identity = await controllerPromise;
      return identity;
    },
    async discard() {},
    reload() {
      windowValue.location.reload?.();
    },
  };
}

export async function startWorkspaceServiceWorkerRuntime({
  container,
  navigatorValue,
  windowValue,
  documentValue,
  pageBuildId,
  createMessageChannel,
  handshakeTimeoutMs = HANDSHAKE_TIMEOUT_MS,
  buildUpdates,
  fetchValue,
  createBroadcastChannel,
  nowValue = Date.now,
}: StartServiceWorkerOptions): Promise<WorkspaceServiceWorkerRuntime> {
  const atRoot = !windowValue.location.pathname.startsWith("/ui/");
  await container.register(
    workerUrl(atRoot, pageBuildId),
    {
      scope: atRoot ? "/" : "/ui/",
      updateViaCache: "none",
    },
  );

  let disposed = false;
  let warmupScheduled = false;
  let handshakeSequence = 0;
  let heartbeatHandle: number | null = null;
  let lastLeaseReportAt = nowValue();
  let resyncInFlight: Promise<void> | null = null;

  const reportLease = (controller: WorkerControllerLike): void => {
    reportPageBuildLease(controller, pageBuildId);
    lastLeaseReportAt = nowValue();
  };

  const inspectController = async () => {
    const sequence = ++handshakeSequence;
    const controller = container.controller;
    if (!controller || disposed) return;
    reportLease(controller);
    const identity = await requestWorkerBuildIdentity(
      controller,
      windowValue,
      createMessageChannel,
      handshakeTimeoutMs,
    );
    if (identity) buildUpdates?.noteControllerIdentity(identity);
    if (
      disposed ||
      sequence !== handshakeSequence ||
      controller !== container.controller ||
      !identity ||
      identity.buildId !== pageBuildId ||
      !identity.cacheReady ||
      warmupScheduled
    ) {
      return;
    }
    warmupScheduled = scheduleWorkspaceOfflineWarmup(
      controller,
      identity,
      navigatorValue,
      windowValue,
      () => controller === container.controller,
    );
  };

  const onControllerChange: EventListener = () => {
    void inspectController();
  };
  const onWorkerMessage: EventListener = (event) => {
    const data = (event as MessageEvent).data;
    if (data?.type === "worker_activated") void inspectController();
  };
  const onVisibilityChange: EventListener = () => {
    if (documentValue.visibilityState !== "visible") return;
    const controller = container.controller;
    if (controller) reportLease(controller);
  };

  const startHeartbeat = (): void => {
    heartbeatHandle = windowValue.setInterval(() => {
      const controller = container.controller;
      if (controller) reportLease(controller);
    }, LEASE_HEARTBEAT_MS);
  };

  /**
   * BFCache 恢复后的运行时重同步。页面被冻结期间，心跳计时器、部署构建
   * 检查和 Worker 握手结果都可能已经过期，因此按固定顺序逐步恢复：
   * 重试持久化并刷新健康状态、强制检查部署构建（只发现，绝不激活
   * 等待中的 Worker 或 reload）、重新握手当前 Worker、补报页面构建
   * 租约；最后，若心跳疑似死亡（超过两个心跳周期未上报租约），仅当
   * 心跳句柄真的丢失时才重建——绝不创建第二个计时器、第二个
   * BroadcastChannel 或第二个监听器。每一步独立吞错，单步失败不得
   * 中断后续步骤；恢复期间的并发 pageshow 共享同一个 in-flight 任务。
   */
  const resyncAfterBfcache = (): Promise<void> => {
    if (disposed) return Promise.resolve();
    if (resyncInFlight) return resyncInFlight;
    const task = (async () => {
      const heartbeatAppearsDead = nowValue() - lastLeaseReportAt > LEASE_HEARTBEAT_DEAD_MS;
      try {
        recordFlushReport(flushReloadPersistence());
      } catch {
        // 持久化重试失败不得中断后续重同步步骤。
      }
      try {
        await buildUpdates?.checkForUpdate({ reason: "bfcache", force: true });
      } catch {
        // 构建检查失败不得阻断握手与租约补报。
      }
      const controller = container.controller;
      if (controller && !disposed) {
        try {
          const identity = await requestWorkerBuildIdentity(
            controller,
            windowValue,
            createMessageChannel,
            handshakeTimeoutMs,
          );
          if (identity) buildUpdates?.noteControllerIdentity(identity);
        } catch {
          // 握手失败仍要补报租约。
        }
        try {
          reportLease(controller);
        } catch {
          // 租约补报失败不影响计时器恢复。
        }
      }
      if (heartbeatAppearsDead && heartbeatHandle === null && !disposed) {
        startHeartbeat();
      }
    })();
    resyncInFlight = task;
    const clearInFlight = (): void => {
      if (resyncInFlight === task) resyncInFlight = null;
    };
    task.then(clearInFlight, clearInFlight);
    return task;
  };

  const onPageShow: EventListener = (event) => {
    if ((event as PageTransitionEvent).persisted !== true) return;
    void resyncAfterBfcache();
  };

  container.addEventListener("controllerchange", onControllerChange);
  container.addEventListener("message", onWorkerMessage);
  documentValue.addEventListener("visibilitychange", onVisibilityChange);
  windowValue.addEventListener("pageshow", onPageShow);
  startHeartbeat();

  const stopBuildUpdates = buildUpdates && fetchValue
    ? buildUpdates.configure(
      {
        fetchValue,
        windowValue: windowValue as unknown as BuildUpdateEnvironment["windowValue"],
        documentValue: documentValue as unknown as BuildUpdateEnvironment["documentValue"],
        createBroadcastChannel,
      },
      createBuildUpdateDriver(container, windowValue, atRoot, createMessageChannel),
    )
    : () => undefined;
  await inspectController();

  return {
    dispose() {
      disposed = true;
      handshakeSequence += 1;
      container.removeEventListener("controllerchange", onControllerChange);
      container.removeEventListener("message", onWorkerMessage);
      documentValue.removeEventListener("visibilitychange", onVisibilityChange);
      windowValue.removeEventListener("pageshow", onPageShow);
      if (heartbeatHandle !== null) {
        windowValue.clearInterval(heartbeatHandle);
        heartbeatHandle = null;
      }
      stopBuildUpdates();
    },
    resyncAfterBfcache,
  };
}
