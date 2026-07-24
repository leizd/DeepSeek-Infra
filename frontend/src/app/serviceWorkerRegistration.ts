import {
  scheduleWorkspaceOfflineWarmup,
  type WorkerBuildIdentity,
} from "./workspaceOfflineWarmup";
import {
  type BuildUpdateEnvironment,
  type BuildUpdateStore,
  type DeployedBuild,
} from "./buildUpdateStore";

const BUILD_ID_PATTERN = /^[0-9a-f]{16}$/;
const ASSET_DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const HANDSHAKE_TIMEOUT_MS = 5000;
const LEASE_HEARTBEAT_MS = 60_000;

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
    const watched = new Set<ServiceWorkerLike>();
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
      if (!installing || watched.has(installing)) return;
      watched.add(installing);
      const onStateChange: EventListener = () => {
        if (installing.state === "redundant") {
          finish(null, new Error("更新 Worker 安装失败"));
          return;
        }
        inspect();
      };
      installing.addEventListener?.("statechange", onStateChange);
    };
    const onUpdateFound: EventListener = () => inspect();
    const cleanup = () => {
      windowValue.clearTimeout(timer);
      registration.removeEventListener?.("updatefound", onUpdateFound);
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
}: StartServiceWorkerOptions): Promise<() => void> {
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

  const inspectController = async () => {
    const sequence = ++handshakeSequence;
    const controller = container.controller;
    if (!controller || disposed) return;
    reportPageBuildLease(controller, pageBuildId);
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
    if (controller) reportPageBuildLease(controller, pageBuildId);
  };

  container.addEventListener("controllerchange", onControllerChange);
  container.addEventListener("message", onWorkerMessage);
  documentValue.addEventListener("visibilitychange", onVisibilityChange);
  const heartbeat = windowValue.setInterval(() => {
    const controller = container.controller;
    if (controller) reportPageBuildLease(controller, pageBuildId);
  }, LEASE_HEARTBEAT_MS);

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

  return () => {
    disposed = true;
    handshakeSequence += 1;
    container.removeEventListener("controllerchange", onControllerChange);
    container.removeEventListener("message", onWorkerMessage);
    documentValue.removeEventListener("visibilitychange", onVisibilityChange);
    windowValue.clearInterval(heartbeat);
    stopBuildUpdates();
  };
}
