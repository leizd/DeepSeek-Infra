import { ApiError } from "../../api/httpClient";
import { putBackupMirror } from "../../api/workspaceBackupApi";
import {
  readActiveWorkspaceEpoch,
  WORKSPACE_RESTORE_FENCE_KEY,
  type StorageLike,
} from "../../domain/conversation/persistence";
import { collectFrontendBackupEnvelope } from "./frontendBackup";

const DEFAULT_SOURCE_VERSION = "4.4.8";
const DEBOUNCE_MS = 2000;
const LEASE_TTL_MS = 8_000;
const HEARTBEAT_MS = 3_000;
const BACKOFF_BASE_MS = 2_000;
const BACKOFF_MAX_MS = 60_000;
const CHANNEL_NAME = "deepseek-backup-mirror";
const LEADER_LEASE_KEY = "deepseek-infra.backup-mirror.leader";
const SEQUENCE_KEY_PREFIX = "deepseek-infra.backup-mirror.sequence.";
const HEAD_GEN_KEY_PREFIX = "deepseek-infra.backup-mirror.head.";
const REPLICA_SESSION_KEY = "deepseek-infra.backup-mirror.replica-id";

export interface BroadcastChannelLike {
  postMessage(message: unknown): void;
  addEventListener(type: "message", listener: (event: { data: unknown }) => void): void;
  removeEventListener(type: "message", listener: (event: { data: unknown }) => void): void;
  close(): void;
}

export type BroadcastChannelFactory = (name: string) => BroadcastChannelLike | null;

export type MirrorChannelMessage =
  | { type: "leader_heartbeat"; replicaId: string; expiresAt: number }
  | { type: "leader_release"; replicaId: string }
  | {
      type: "mirror_uploaded";
      profileId: string;
      generationId: string;
      envelopeDigest: string;
      sequence: number;
      sourceEpoch: string;
    };

export interface MirrorLeaderLease {
  schemaVersion: 1;
  replicaId: string;
  expiresAt: number;
  claimedAt: number;
}

export type MirrorTimerHandle = number | ReturnType<typeof setTimeout>;

export interface BackupMirrorTimers {
  setTimeout: (handler: () => void, timeout?: number) => MirrorTimerHandle;
  clearTimeout: (handle: MirrorTimerHandle) => void;
  setInterval: (handler: () => void, timeout?: number) => MirrorTimerHandle;
  clearInterval: (handle: MirrorTimerHandle) => void;
}

export interface BackupMirrorEnvironment {
  storage?: StorageLike;
  sessionStorage?: StorageLike | null;
  createBroadcastChannel?: BroadcastChannelFactory;
  now?: () => number;
  online?: () => boolean;
  timers?: BackupMirrorTimers;
}

let inFlight: Promise<void> | null = null;
let scheduledHandle: MirrorTimerHandle | null = null;
let heartbeatHandle: MirrorTimerHandle | null = null;
let backoffHandle: MirrorTimerHandle | null = null;
let lastUploadedDigest = "";
let lastUploadedEpoch = "";
let backoffAttempt = 0;
let frozenByRestore = false;
let channel: BroadcastChannelLike | null | undefined;
let activeEnv: Required<Pick<BackupMirrorEnvironment, "now" | "online">> & {
  storage: StorageLike;
  sessionStorage: StorageLike | null;
  createBroadcastChannel: BroadcastChannelFactory;
  timers: BackupMirrorTimers;
  sourceVersion: string;
} | null = null;
const uploadedListeners = new Set<(message: Extract<MirrorChannelMessage, { type: "mirror_uploaded" }>) => void>();

const defaultTimers: BackupMirrorTimers = {
  setTimeout: (handler, timeout) => globalThis.setTimeout(handler, timeout) as MirrorTimerHandle,
  clearTimeout: (handle) => {
    globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>);
  },
  setInterval: (handler, timeout) => globalThis.setInterval(handler, timeout) as MirrorTimerHandle,
  clearInterval: (handle) => {
    globalThis.clearInterval(handle as ReturnType<typeof setInterval>);
  },
};

function defaultStorage(): StorageLike {
  const globalWindow = (globalThis as { window?: Window }).window;
  if (!globalWindow) throw new Error("备份镜像需要浏览器环境");
  return globalWindow.localStorage;
}

function defaultSessionStorage(): StorageLike | null {
  const globalWindow = (globalThis as { window?: Window }).window;
  if (!globalWindow) return null;
  try {
    return globalWindow.sessionStorage;
  } catch {
    return null;
  }
}

function defaultChannelFactory(name: string): BroadcastChannelLike | null {
  if (typeof BroadcastChannel === "undefined") return null;
  try {
    return new BroadcastChannel(name);
  } catch {
    return null;
  }
}

function defaultNow(): number {
  return Date.now();
}

function defaultOnline(): boolean {
  const nav = (globalThis as { navigator?: Navigator }).navigator;
  return nav?.onLine !== false;
}

function randomReplicaId(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") return `mirror_${cryptoApi.randomUUID().replace(/-/g, "").slice(0, 24)}`;
  const bytes = new Uint8Array(12);
  if (typeof cryptoApi?.getRandomValues === "function") cryptoApi.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  return `mirror_${[...bytes].map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function isStorageLike(value: unknown): value is StorageLike {
  return Boolean(value && typeof value === "object" && typeof (value as StorageLike).getItem === "function");
}

function resolveEnv(
  env: BackupMirrorEnvironment | StorageLike = {},
  sourceVersion: string = DEFAULT_SOURCE_VERSION,
): NonNullable<typeof activeEnv> {
  const options: BackupMirrorEnvironment = isStorageLike(env) ? { storage: env } : env;
  return {
    storage: options.storage ?? defaultStorage(),
    sessionStorage: options.sessionStorage === undefined ? defaultSessionStorage() : options.sessionStorage,
    createBroadcastChannel: options.createBroadcastChannel ?? defaultChannelFactory,
    now: options.now ?? defaultNow,
    online: options.online ?? defaultOnline,
    timers: options.timers ?? defaultTimers,
    sourceVersion,
  };
}

export function clientReplicaId(sessionStorage: StorageLike | null = defaultSessionStorage()): string {
  try {
    const existing = sessionStorage?.getItem(REPLICA_SESSION_KEY)?.trim();
    if (existing && existing.length >= 8 && existing.length <= 64) return existing;
  } catch {
    // sessionStorage may be blocked
  }
  const created = randomReplicaId();
  try {
    sessionStorage?.setItem(REPLICA_SESSION_KEY, created);
  } catch {
    // memory-only for this call site when storage is unavailable
  }
  return created;
}

export async function backupMirrorProfileId(epoch: string): Promise<string> {
  const bytes = new TextEncoder().encode(`frontend-mirror:${epoch}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hex = [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, "0")).join("");
  return `mirror_${hex.slice(0, 16)}`;
}

function parseLease(raw: string | null): MirrorLeaderLease | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<MirrorLeaderLease>;
    if (typeof value.replicaId !== "string" || !value.replicaId) return null;
    if (typeof value.expiresAt !== "number" || !Number.isFinite(value.expiresAt)) return null;
    if (typeof value.claimedAt !== "number" || !Number.isFinite(value.claimedAt)) return null;
    return {
      schemaVersion: 1,
      replicaId: value.replicaId.slice(0, 64),
      expiresAt: value.expiresAt,
      claimedAt: value.claimedAt,
    };
  } catch {
    return null;
  }
}

function readLease(storage: StorageLike): MirrorLeaderLease | null {
  try {
    return parseLease(storage.getItem(LEADER_LEASE_KEY));
  } catch {
    return null;
  }
}

function writeLease(storage: StorageLike, lease: MirrorLeaderLease): void {
  storage.setItem(LEADER_LEASE_KEY, JSON.stringify(lease));
}

function clearLease(storage: StorageLike, replicaId: string): void {
  const current = readLease(storage);
  if (current && current.replicaId !== replicaId) return;
  try {
    storage.removeItem(LEADER_LEASE_KEY);
  } catch {
    // ignore
  }
}

function isRestoreFenced(storage: StorageLike): boolean {
  if (frozenByRestore) return true;
  try {
    return storage.getItem(WORKSPACE_RESTORE_FENCE_KEY) !== null;
  } catch {
    return true;
  }
}

function readSequence(storage: StorageLike, profileId: string): number {
  try {
    const raw = storage.getItem(`${SEQUENCE_KEY_PREFIX}${profileId}`);
    const value = raw == null ? 0 : Number(raw);
    return Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
  } catch {
    return 0;
  }
}

function writeSequence(storage: StorageLike, profileId: string, sequence: number): void {
  try {
    storage.setItem(`${SEQUENCE_KEY_PREFIX}${profileId}`, String(Math.max(0, Math.floor(sequence))));
  } catch {
    // best-effort
  }
}

function readHeadGeneration(storage: StorageLike, profileId: string): string {
  try {
    return storage.getItem(`${HEAD_GEN_KEY_PREFIX}${profileId}`) ?? "";
  } catch {
    return "";
  }
}

function writeHeadGeneration(storage: StorageLike, profileId: string, generationId: string): void {
  try {
    if (!generationId) storage.removeItem(`${HEAD_GEN_KEY_PREFIX}${profileId}`);
    else storage.setItem(`${HEAD_GEN_KEY_PREFIX}${profileId}`, generationId);
  } catch {
    // best-effort
  }
}

function ensureChannel(factory: BroadcastChannelFactory): BroadcastChannelLike | null {
  if (channel !== undefined) return channel;
  try {
    channel = factory(CHANNEL_NAME);
  } catch {
    channel = null;
  }
  if (channel) {
    try {
      channel.addEventListener("message", onChannelMessage);
    } catch {
      channel = null;
    }
  }
  return channel;
}

function postChannel(message: MirrorChannelMessage, factory: BroadcastChannelFactory): void {
  try {
    ensureChannel(factory)?.postMessage(message);
  } catch {
    // cross-tab notify is best-effort
  }
}

function parseChannelMessage(data: unknown): MirrorChannelMessage | null {
  if (!data || typeof data !== "object") return null;
  const message = data as Partial<MirrorChannelMessage> & { type?: unknown };
  if (message.type === "leader_heartbeat") {
    if (typeof message.replicaId !== "string" || !message.replicaId) return null;
    if (typeof message.expiresAt !== "number" || !Number.isFinite(message.expiresAt)) return null;
    return { type: "leader_heartbeat", replicaId: message.replicaId, expiresAt: message.expiresAt };
  }
  if (message.type === "leader_release") {
    if (typeof message.replicaId !== "string" || !message.replicaId) return null;
    return { type: "leader_release", replicaId: message.replicaId };
  }
  if (message.type === "mirror_uploaded") {
    if (typeof message.profileId !== "string" || !message.profileId) return null;
    if (typeof message.generationId !== "string" || !message.generationId) return null;
    if (typeof message.envelopeDigest !== "string") return null;
    if (typeof message.sequence !== "number" || !Number.isFinite(message.sequence)) return null;
    if (typeof message.sourceEpoch !== "string") return null;
    return {
      type: "mirror_uploaded",
      profileId: message.profileId,
      generationId: message.generationId,
      envelopeDigest: message.envelopeDigest,
      sequence: message.sequence,
      sourceEpoch: message.sourceEpoch,
    };
  }
  return null;
}

function onChannelMessage(event: { data: unknown }): void {
  const message = parseChannelMessage(event.data);
  if (!message) return;
  if (message.type === "mirror_uploaded") {
    lastUploadedDigest = message.envelopeDigest;
    lastUploadedEpoch = message.sourceEpoch;
    uploadedListeners.forEach((listener) => listener(message));
  }
}

let pageListenersBound = false;

function bindPageListeners(env: NonNullable<typeof activeEnv>): void {
  if (pageListenersBound) return;
  const globalWindow = (globalThis as { window?: Window }).window;
  if (!globalWindow) return;
  pageListenersBound = true;
  const release = (): void => {
    try {
      const storage = env.storage;
      const replica = clientReplicaId(env.sessionStorage);
      if (isLeader(storage, replica, env.now())) {
        clearLease(storage, replica);
        postChannel({ type: "leader_release", replicaId: replica }, env.createBroadcastChannel);
      }
    } catch {
      // unload path must never throw
    }
    stopHeartbeat(env.timers);
  };
  globalWindow.addEventListener("pagehide", release);
  globalWindow.addEventListener("beforeunload", release);
  globalWindow.addEventListener("online", () => {
    backoffAttempt = 0;
  });
  globalWindow.addEventListener("storage", (event: StorageEvent) => {
    if (event.key === WORKSPACE_RESTORE_FENCE_KEY) {
      frozenByRestore = event.newValue !== null;
    }
  });
}

function stopHeartbeat(timers: BackupMirrorTimers = defaultTimers): void {
  if (heartbeatHandle !== null) {
    timers.clearInterval(heartbeatHandle);
    heartbeatHandle = null;
  }
}

function startHeartbeat(env: NonNullable<typeof activeEnv>, replicaId: string): void {
  stopHeartbeat(env.timers);
  heartbeatHandle = env.timers.setInterval(() => {
    renewLeadership(env, replicaId);
  }, HEARTBEAT_MS);
}

export function isLeader(storage: StorageLike, replicaId: string, nowMs: number = defaultNow()): boolean {
  const lease = readLease(storage);
  return Boolean(lease && lease.replicaId === replicaId && lease.expiresAt > nowMs);
}

export function claimLeadership(
  storage: StorageLike,
  replicaId: string,
  env: BackupMirrorEnvironment = {},
): boolean {
  const resolved = resolveEnv({ ...env, storage });
  activeEnv = { ...resolved, sourceVersion: resolved.sourceVersion };
  const nowMs = resolved.now();
  const current = readLease(storage);
  if (current && current.expiresAt > nowMs && current.replicaId !== replicaId) {
    return false;
  }
  const lease: MirrorLeaderLease = {
    schemaVersion: 1,
    replicaId,
    claimedAt: current?.replicaId === replicaId ? current.claimedAt : nowMs,
    expiresAt: nowMs + LEASE_TTL_MS,
  };
  try {
    writeLease(storage, lease);
  } catch {
    return false;
  }
  const confirmed = readLease(storage);
  if (!confirmed || confirmed.replicaId !== replicaId) return false;
  postChannel(
    { type: "leader_heartbeat", replicaId, expiresAt: lease.expiresAt },
    resolved.createBroadcastChannel,
  );
  startHeartbeat(resolved, replicaId);
  bindPageListeners(resolved);
  return true;
}

function renewLeadership(env: NonNullable<typeof activeEnv>, replicaId: string): boolean {
  if (!isLeader(env.storage, replicaId, env.now())) {
    stopHeartbeat(env.timers);
    return false;
  }
  return claimLeadership(env.storage, replicaId, env);
}

function ensureLeadership(env: NonNullable<typeof activeEnv>): { replicaId: string; leader: boolean } {
  const replicaId = clientReplicaId(env.sessionStorage);
  ensureChannel(env.createBroadcastChannel);
  bindPageListeners(env);
  if (isLeader(env.storage, replicaId, env.now())) {
    renewLeadership(env, replicaId);
    return { replicaId, leader: true };
  }
  const leader = claimLeadership(env.storage, replicaId, env);
  return { replicaId, leader };
}

function scheduleBackoff(env: NonNullable<typeof activeEnv>): void {
  if (backoffHandle !== null) env.timers.clearTimeout(backoffHandle);
  const delay = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** Math.min(backoffAttempt, 5));
  backoffAttempt += 1;
  backoffHandle = env.timers.setTimeout(() => {
    backoffHandle = null;
    void uploadBackupMirror(env.sourceVersion, env);
  }, delay);
}

function clearBackoff(timers: BackupMirrorTimers = activeEnv?.timers ?? defaultTimers): void {
  if (backoffHandle !== null) {
    timers.clearTimeout(backoffHandle);
    backoffHandle = null;
  }
  backoffAttempt = 0;
}

export function freezeBackupMirrorForRestore(): void {
  frozenByRestore = true;
  if (scheduledHandle !== null) {
    (activeEnv?.timers ?? defaultTimers).clearTimeout(scheduledHandle);
    scheduledHandle = null;
  }
  clearBackoff();
}

export function unfreezeBackupMirrorAfterRestore(): void {
  frozenByRestore = false;
}

export function onBackupMirrorUploaded(
  listener: (message: Extract<MirrorChannelMessage, { type: "mirror_uploaded" }>) => void,
): () => void {
  uploadedListeners.add(listener);
  return () => {
    uploadedListeners.delete(listener);
  };
}

export function uploadBackupMirror(
  sourceVersion: string = DEFAULT_SOURCE_VERSION,
  env: BackupMirrorEnvironment | StorageLike = {},
): Promise<void> {
  const resolved = resolveEnv(env, sourceVersion);
  activeEnv = resolved;

  if (inFlight) return inFlight;

  const task = (async () => {
    try {
      if (isRestoreFenced(resolved.storage)) return;
      if (!resolved.online()) {
        scheduleBackoff(resolved);
        return;
      }
      const { replicaId, leader } = ensureLeadership(resolved);
      if (!leader) return;

      const epoch = readActiveWorkspaceEpoch(resolved.storage);
      const envelope = await collectFrontendBackupEnvelope(sourceVersion, false, resolved.storage);
      if (envelope.digest === lastUploadedDigest && epoch === lastUploadedEpoch) return;

      const profileId = await backupMirrorProfileId(epoch);
      let sequence = readSequence(resolved.storage, profileId) + 1;
      writeSequence(resolved.storage, profileId, sequence);
      let expectedHead = readHeadGeneration(resolved.storage, profileId);
      let attempt = 0;
      while (attempt < 3) {
        attempt += 1;
        if (!renewLeadership(resolved, replicaId)) return;
        if (isRestoreFenced(resolved.storage)) return;
        try {
          const metadata = await putBackupMirror(profileId, {
            sourceEpoch: epoch,
            acknowledgedAt: new Date(resolved.now()).toISOString(),
            envelope,
            clientReplicaId: replicaId,
            clientSequence: sequence,
            expectedHeadGenerationId: expectedHead || undefined,
          });
          const generationId = typeof metadata.generationId === "string" ? metadata.generationId : "";
          if (generationId) writeHeadGeneration(resolved.storage, profileId, generationId);
          if (typeof metadata.clientSequence === "number") {
            writeSequence(resolved.storage, profileId, metadata.clientSequence);
          }
          lastUploadedDigest = envelope.digest;
          lastUploadedEpoch = epoch;
          clearBackoff(resolved.timers);
          const uploaded: Extract<MirrorChannelMessage, { type: "mirror_uploaded" }> = {
            type: "mirror_uploaded",
            profileId,
            generationId: generationId || `seq_${sequence}`,
            envelopeDigest: envelope.digest,
            sequence,
            sourceEpoch: epoch,
          };
          postChannel(uploaded, resolved.createBroadcastChannel);
          uploadedListeners.forEach((listener) => listener(uploaded));
          return;
        } catch (error) {
          if (error instanceof ApiError) {
            const message = error.message || "";
            if (error.status === 423 || message.includes("fenced")) {
              freezeBackupMirrorForRestore();
              return;
            }
            if (message.includes("mirror-stale-epoch")) {
              lastUploadedDigest = "";
              lastUploadedEpoch = "";
              return;
            }
            if (message.includes("mirror-stale-sequence")) {
              sequence = Math.max(sequence + 1, resolved.now());
              writeSequence(resolved.storage, profileId, sequence);
              continue;
            }
            if (message.includes("mirror-head-conflict")) {
              expectedHead = "";
              writeHeadGeneration(resolved.storage, profileId, "");
              sequence += 1;
              writeSequence(resolved.storage, profileId, sequence);
              continue;
            }
            if (error.status >= 500 || error.status === 0 || error.status === 408 || error.status === 429) {
              scheduleBackoff(resolved);
              return;
            }
            console.warn("备份镜像上传失败", error);
            return;
          }
          console.warn("备份镜像上传失败", error);
          scheduleBackoff(resolved);
          return;
        }
      }
    } catch (error) {
      console.warn("备份镜像上传失败", error);
      scheduleBackoff(resolved);
    }
  })();

  inFlight = task;
  return task.finally(() => {
    if (inFlight === task) inFlight = null;
  });
}

export function scheduleBackupMirrorUpload(
  sourceVersion: string = DEFAULT_SOURCE_VERSION,
  delayMs: number = DEBOUNCE_MS,
  env?: BackupMirrorEnvironment | StorageLike,
): void {
  if (frozenByRestore) return;
  const resolved = resolveEnv(env ?? {}, sourceVersion);
  activeEnv = resolved;
  if (scheduledHandle !== null) resolved.timers.clearTimeout(scheduledHandle);
  scheduledHandle = resolved.timers.setTimeout(() => {
    scheduledHandle = null;
    void uploadBackupMirror(sourceVersion, resolved);
  }, delayMs);
}

export function resetBackupMirrorStateForTests(): void {
  lastUploadedDigest = "";
  lastUploadedEpoch = "";
  backoffAttempt = 0;
  frozenByRestore = false;
  const timers = activeEnv?.timers ?? defaultTimers;
  if (scheduledHandle !== null) {
    timers.clearTimeout(scheduledHandle);
    scheduledHandle = null;
  }
  clearBackoff(timers);
  stopHeartbeat(timers);
  if (channel) {
    try {
      channel.removeEventListener("message", onChannelMessage);
      channel.close();
    } catch {
      // ignore
    }
  }
  channel = undefined;
  uploadedListeners.clear();
  inFlight = null;
  activeEnv = null;
  pageListenersBound = false;
}

export const backupMirrorStorageKeys = {
  leaderLease: LEADER_LEASE_KEY,
  sequencePrefix: SEQUENCE_KEY_PREFIX,
  headGenerationPrefix: HEAD_GEN_KEY_PREFIX,
  replicaSession: REPLICA_SESSION_KEY,
  channelName: CHANNEL_NAME,
} as const;
