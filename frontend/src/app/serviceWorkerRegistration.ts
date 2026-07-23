import {
  scheduleWorkspaceOfflineWarmup,
  type WorkerBuildIdentity,
} from "./workspaceOfflineWarmup";

const BUILD_ID_PATTERN = /^[0-9a-f]{16}$/;
const ASSET_DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const HANDSHAKE_TIMEOUT_MS = 5000;
const LEASE_HEARTBEAT_MS = 60_000;

interface WorkerControllerLike {
  postMessage(message: unknown, transfer?: Transferable[]): void;
}

interface ServiceWorkerContainerLike {
  controller: WorkerControllerLike | null;
  register(scriptURL: string, options: RegistrationOptions): Promise<unknown>;
  addEventListener(type: "controllerchange" | "message", listener: EventListener): void;
  removeEventListener(type: "controllerchange" | "message", listener: EventListener): void;
}

interface RuntimeWindowLike {
  location: { pathname: string };
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

export async function startWorkspaceServiceWorkerRuntime({
  container,
  navigatorValue,
  windowValue,
  documentValue,
  pageBuildId,
  createMessageChannel,
  handshakeTimeoutMs = HANDSHAKE_TIMEOUT_MS,
}: StartServiceWorkerOptions): Promise<() => void> {
  const atRoot = !windowValue.location.pathname.startsWith("/ui/");
  await container.register(
    atRoot ? `/sw-${pageBuildId}.js` : `/ui/sw-${pageBuildId}.js`,
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

  await inspectController();

  return () => {
    disposed = true;
    handshakeSequence += 1;
    container.removeEventListener("controllerchange", onControllerChange);
    container.removeEventListener("message", onWorkerMessage);
    documentValue.removeEventListener("visibilitychange", onVisibilityChange);
    windowValue.clearInterval(heartbeat);
  };
}
