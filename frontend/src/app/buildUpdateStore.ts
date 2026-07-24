import {
  flushReloadPersistence,
  getReloadBlockerSnapshot,
  subscribeReloadBlockers,
  type ReloadBlocker,
} from "./reloadBlockers";
import type { WorkerBuildIdentity } from "./workspaceOfflineWarmup";

const BUILD_ID_PATTERN = /^[0-9a-f]{16}$/;
const ASSET_DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const SOURCE_REVISION_PATTERN = /^[0-9A-Za-z._-]{7,160}$/;
const UPDATE_INTERVAL_MS = 5 * 60_000;
const CHECK_TIMEOUT_MS = 12_000;

export interface DeployedBuild {
  schemaVersion: 1;
  version: string;
  sourceRevision: string;
  buildId: string;
  assetSetDigest: string;
}

export type BuildUpdatePhase =
  | "current"
  | "checking"
  | "available"
  | "installing"
  | "ready"
  | "blocked"
  | "activating"
  | "reload-required"
  | "error";

export interface BuildUpdateSnapshot {
  phase: BuildUpdatePhase;
  pageBuildId: string;
  controllerBuildId?: string;
  targetBuildId?: string;
  targetVersion?: string;
  targetAssetSetDigest?: string;
  error?: string;
  deferred?: boolean;
}

export type BuildUpdateMessage =
  | { type: "build_available"; buildId: string; version: string }
  | { type: "build_staged"; buildId: string }
  | { type: "build_activated"; buildId: string };

export type ActivationStage =
  | "revalidating"
  | "flushing"
  | "worker-activation-requested"
  | "controller-verified"
  | "reload-issued";

export interface BuildUpdateDriver {
  stage(build: DeployedBuild): Promise<WorkerBuildIdentity>;
  activate(build: DeployedBuild): Promise<WorkerBuildIdentity>;
  discard?(build: DeployedBuild): Promise<void>;
  reload(): void;
}

interface BroadcastChannelLike {
  postMessage(message: BuildUpdateMessage): void;
  close(): void;
  addEventListener(type: "message", listener: EventListener): void;
  removeEventListener(type: "message", listener: EventListener): void;
}

export type BuildUpdateCheckReason = "startup" | "interval" | "online" | "visible" | "manual";

export interface BuildUpdateCheckOptions {
  reason?: BuildUpdateCheckReason;
  force?: boolean;
}

export interface BuildUpdateEnvironment {
  fetchValue: typeof fetch;
  windowValue: Pick<
    Window,
    "addEventListener" | "removeEventListener" | "setInterval" | "clearInterval"
  >;
  documentValue: Pick<Document, "visibilityState" | "addEventListener" | "removeEventListener">;
  createBroadcastChannel?: (name: string) => BroadcastChannelLike;
  checkTimeoutMs?: number;
}

function validDeployedBuild(value: unknown): value is DeployedBuild {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<DeployedBuild>;
  return candidate.schemaVersion === 1
    && typeof candidate.version === "string"
    && candidate.version.length > 0
    && candidate.version.length <= 32
    && typeof candidate.sourceRevision === "string"
    && SOURCE_REVISION_PATTERN.test(candidate.sourceRevision)
    && typeof candidate.buildId === "string"
    && BUILD_ID_PATTERN.test(candidate.buildId)
    && typeof candidate.assetSetDigest === "string"
    && ASSET_DIGEST_PATTERN.test(candidate.assetSetDigest);
}

export function parseDeployedBuild(value: unknown): DeployedBuild | null {
  return validDeployedBuild(value) ? value : null;
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error && reason.message ? reason.message : "检查更新失败";
}

export class BuildUpdateStore {
  private snapshot: BuildUpdateSnapshot;
  private readonly listeners = new Set<() => void>();
  private environment: BuildUpdateEnvironment | null = null;
  private driver: BuildUpdateDriver | null = null;
  private channel: BroadcastChannelLike | null = null;
  private intervalHandle: number | null = null;
  private checkPromise: Promise<DeployedBuild | null> | null = null;
  private checkController: AbortController | null = null;
  private checkSequence = 0;
  private target: DeployedBuild | null = null;
  private targetGeneration = 0;
  private activationTask: Promise<void> | null = null;
  private activationGeneration = 0;
  private activationStage: ActivationStage | null = null;
  private reloadIssued = false;
  private activatedBroadcastFor: string | null = null;
  private autoActivate = false;
  private stopped = false;
  private unsubscribeBlockers: (() => void) | null = null;

  constructor(readonly pageBuildId: string) {
    this.snapshot = { phase: "current", pageBuildId };
  }

  getSnapshot = (): BuildUpdateSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private publish(patch: Partial<BuildUpdateSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.listeners.forEach((listener) => listener());
  }

  configure(environment: BuildUpdateEnvironment, driver: BuildUpdateDriver): () => void {
    this.stop();
    this.stopped = false;
    this.environment = environment;
    this.driver = driver;
    this.publish({
      phase: "current",
      controllerBuildId: undefined,
      targetBuildId: undefined,
      targetVersion: undefined,
      targetAssetSetDigest: undefined,
      error: undefined,
      deferred: false,
    });
    this.channel = environment.createBroadcastChannel?.("deepseek-build-updates") ?? null;
    this.channel?.addEventListener("message", this.onBroadcastMessage);
    environment.windowValue.addEventListener("online", this.onOnline);
    environment.documentValue.addEventListener("visibilitychange", this.onVisibility);
    this.intervalHandle = environment.windowValue.setInterval(() => {
      if (environment.documentValue.visibilityState === "visible") {
        void this.checkForUpdate({ reason: "interval" });
      }
    }, UPDATE_INTERVAL_MS);
    this.unsubscribeBlockers = subscribeReloadBlockers(this.onBlockersChanged);
    void this.checkForUpdate({ reason: "startup" });
    return () => {
      this.stop();
    };
  }

  stop(): void {
    this.stopped = true;
    this.checkSequence += 1;
    this.checkController?.abort();
    this.checkController = null;
    this.checkPromise = null;
    this.target = null;
    this.targetGeneration += 1;
    this.activationTask = null;
    this.activationGeneration += 1;
    this.activationStage = null;
    this.reloadIssued = false;
    this.activatedBroadcastFor = null;
    this.autoActivate = false;
    const environment = this.environment;
    if (environment) {
      environment.windowValue.removeEventListener("online", this.onOnline);
      environment.documentValue.removeEventListener("visibilitychange", this.onVisibility);
      if (this.intervalHandle !== null) environment.windowValue.clearInterval(this.intervalHandle);
    }
    this.intervalHandle = null;
    this.channel?.removeEventListener("message", this.onBroadcastMessage);
    this.channel?.close();
    this.channel = null;
    this.unsubscribeBlockers?.();
    this.unsubscribeBlockers = null;
    this.environment = null;
    this.driver = null;
  }

  private onOnline: EventListener = () => {
    void this.checkForUpdate({ reason: "online", force: true });
  };

  private onVisibility: EventListener = () => {
    if (this.environment?.documentValue.visibilityState === "visible") {
      void this.checkForUpdate({ reason: "visible" });
    }
  };

  private onBroadcastMessage: EventListener = (event) => {
    const message = (event as MessageEvent<BuildUpdateMessage>).data;
    if (!message || !BUILD_ID_PATTERN.test(message.buildId) || message.buildId === this.pageBuildId) return;
    if (message.type === "build_available" || message.type === "build_staged") {
      void this.checkForUpdate();
      return;
    }
    if (message.type === "build_activated") {
      if (this.target?.buildId === message.buildId) {
        this.publish({ phase: "reload-required", controllerBuildId: message.buildId, deferred: false });
      } else {
        void this.checkForUpdate();
      }
    }
  };

  private onBlockersChanged = (): void => {
    if (!this.autoActivate || getReloadBlockerSnapshot().length) return;
    this.autoActivate = false;
    void this.activateWhenReady();
  };

  async checkForUpdate(options: BuildUpdateCheckOptions = {}): Promise<DeployedBuild | null> {
    const environment = this.environment;
    if (!environment || this.stopped) return null;
    if (this.checkPromise) {
      if (!options.force) return this.checkPromise;
      // 手动或 online 恢复的检查可以替换一个挂起的请求；旧请求的
      // catch/finally 通过 sequence 与 controller 身份判定，不会清理新请求。
      this.checkSequence += 1;
      this.checkController?.abort();
      this.checkController = null;
      this.checkPromise = null;
    }
    const previous = this.snapshot;
    const sequence = ++this.checkSequence;
    const controller = new AbortController();
    this.checkController = controller;
    this.publish({ phase: "checking", error: undefined });
    const timeoutMs = environment.checkTimeoutMs ?? CHECK_TIMEOUT_MS;
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    this.checkPromise = (async () => {
      try {
        const response = await environment.fetchValue("/ui/workspace-assets.json", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`检查更新失败（HTTP ${response.status}）`);
        const build = parseDeployedBuild(await response.json());
        if (sequence !== this.checkSequence || this.stopped) return null;
        if (!build) throw new Error("服务器构建指针格式无效");
        if (build.buildId === this.pageBuildId) {
          if (this.target) {
            this.target = null;
            this.targetGeneration += 1;
          }
          this.publish({
            phase: "current",
            targetBuildId: undefined,
            targetVersion: undefined,
            targetAssetSetDigest: undefined,
            error: undefined,
            deferred: false,
          });
          return build;
        }
        const changed = this.target?.buildId !== build.buildId
          || this.target.assetSetDigest !== build.assetSetDigest;
        if (changed || previous.phase === "error") {
          const superseded = changed ? this.target : null;
          this.target = build;
          this.targetGeneration += 1;
          if (superseded) void this.driver?.discard?.(superseded);
          this.publish({
            phase: "available",
            targetBuildId: build.buildId,
            targetVersion: build.version,
            targetAssetSetDigest: build.assetSetDigest,
            error: undefined,
            deferred: false,
          });
          if (changed) {
            this.channel?.postMessage({
              type: "build_available",
              buildId: build.buildId,
              version: build.version,
            });
          }
          void this.stage(build, this.targetGeneration);
        } else if (previous.phase !== "checking") {
          this.publish({ phase: previous.phase, error: previous.error });
        } else {
          this.publish({ phase: "available", error: undefined });
        }
        return build;
      } catch (reason) {
        if (sequence !== this.checkSequence || this.stopped) return null;
        // 相同 sequence 下的 abort 只会来自检查超时；stop()/force 替换
        // 都会先推进 sequence。
        const timedOut = controller.signal.aborted;
        if (this.target) {
          // 已确认的 target 不因检查失败或超时而降级。
          this.publish({ phase: previous.phase === "checking" ? "available" : previous.phase, error: undefined });
        } else if (previous.phase === "current") {
          this.publish({ phase: "current", error: undefined });
        } else {
          this.publish({ phase: "error", error: timedOut ? "检查更新超时" : errorMessage(reason) });
        }
        return null;
      } finally {
        clearTimeout(timer);
        if (this.checkController === controller) {
          this.checkPromise = null;
          this.checkController = null;
        }
      }
    })();
    return this.checkPromise;
  }

  private async stage(build: DeployedBuild, generation: number): Promise<void> {
    const driver = this.driver;
    if (!driver) return;
    this.publish({ phase: "installing" });
    try {
      const identity = await driver.stage(build);
      if (
        generation !== this.targetGeneration
        || this.target?.buildId !== build.buildId
        || identity.buildId !== build.buildId
        || identity.assetSetDigest !== build.assetSetDigest
        || !identity.cacheReady
      ) {
        return;
      }
      this.publish({ phase: "ready", error: undefined });
      this.channel?.postMessage({ type: "build_staged", buildId: build.buildId });
    } catch (reason) {
      if (generation !== this.targetGeneration) return;
      this.publish({ phase: "error", error: errorMessage(reason) });
    }
  }

  defer(): void {
    this.autoActivate = false;
    if (
      this.activationTask
      && (this.activationStage === "worker-activation-requested"
        || this.activationStage === "controller-verified"
        || this.activationStage === "reload-issued")
    ) {
      // activate_build 已发出，事务不可撤销：不能伪装成已取消。
      return;
    }
    if (this.activationTask) {
      // 尚在复检或持久化阶段，取消这次激活事务。
      this.activationGeneration += 1;
    }
    this.publish({ deferred: true });
  }

  activateWhenReady(): Promise<void> {
    if (this.activationTask) {
      return this.activationTask;
    }
    if (getReloadBlockerSnapshot().length) {
      this.autoActivate = true;
      this.publish({ phase: "blocked", deferred: false });
      return Promise.resolve();
    }
    const generation = ++this.activationGeneration;
    const task = this.runActivation(generation).finally(() => {
      if (this.activationTask === task) {
        this.activationTask = null;
        this.activationStage = null;
      }
    });
    this.activationTask = task;
    return task;
  }

  private activationCurrent(generation: number, target: DeployedBuild): boolean {
    return generation === this.activationGeneration
      && this.target?.buildId === target.buildId
      && this.target.assetSetDigest === target.assetSetDigest;
  }

  private async runActivation(generation: number): Promise<void> {
    const target = this.target;
    const driver = this.driver;
    if (!target || !driver) return;
    this.activationStage = "revalidating";
    const confirmed = await this.checkForUpdate();
    if (!this.activationCurrent(generation, target)) return;
    if (!confirmed || confirmed.buildId !== target.buildId || confirmed.assetSetDigest !== target.assetSetDigest) {
      return;
    }
    const blockers = getReloadBlockerSnapshot();
    if (blockers.length) {
      this.autoActivate = true;
      this.publish({ phase: "blocked" });
      return;
    }
    this.activationStage = "flushing";
    if (!flushReloadPersistence()) {
      this.publish({ phase: "error", error: "本地状态保存失败，已取消重新加载" });
      return;
    }
    if (getReloadBlockerSnapshot().length) {
      this.autoActivate = true;
      this.publish({ phase: "blocked" });
      return;
    }
    this.publish({ phase: "activating", error: undefined, deferred: false });
    this.activationStage = "worker-activation-requested";
    try {
      const identity = await driver.activate(target);
      if (!this.activationCurrent(generation, target)) return;
      if (
        identity.buildId !== target.buildId
        || identity.assetSetDigest !== target.assetSetDigest
        || !identity.cacheReady
      ) {
        this.publish({ phase: "error", error: "新 Worker 身份或缓存状态不匹配" });
        return;
      }
      this.activationStage = "controller-verified";
      this.publish({ controllerBuildId: identity.buildId });
      if (this.activatedBroadcastFor !== identity.buildId) {
        this.activatedBroadcastFor = identity.buildId;
        this.channel?.postMessage({ type: "build_activated", buildId: identity.buildId });
      }
      if (getReloadBlockerSnapshot().length) {
        this.publish({ phase: "reload-required" });
        return;
      }
      if (!flushReloadPersistence()) {
        this.publish({ phase: "reload-required", error: "本地状态保存失败，请重试" });
        return;
      }
      if (getReloadBlockerSnapshot().length) {
        this.publish({ phase: "reload-required" });
        return;
      }
      if (this.reloadIssued) return;
      this.reloadIssued = true;
      this.activationStage = "reload-issued";
      driver.reload();
    } catch (reason) {
      if (!this.activationCurrent(generation, target)) return;
      this.publish({ phase: "error", error: errorMessage(reason) });
    }
  }

  noteControllerIdentity(identity: WorkerBuildIdentity): void {
    this.publish({ controllerBuildId: identity.buildId });
    if (identity.buildId === this.pageBuildId) return;
    if (
      this.target
      && identity.buildId === this.target.buildId
      && identity.assetSetDigest === this.target.assetSetDigest
      && identity.cacheReady
    ) {
      this.publish({ phase: "reload-required" });
      return;
    }
    void this.checkForUpdate();
  }

  blockers(): readonly ReloadBlocker[] {
    return getReloadBlockerSnapshot();
  }
}

const compiledPageBuildId = typeof __APP_BUILD_ID__ === "string" ? __APP_BUILD_ID__ : "0000000000000000";
export const buildUpdateStore = new BuildUpdateStore(compiledPageBuildId);
