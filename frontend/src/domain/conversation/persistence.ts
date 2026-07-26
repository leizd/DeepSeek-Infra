import type { ChatMessage } from "../chat/types";
import {
  classifyStorageError,
  storageUnavailableFailure,
  verificationFailure,
  type PersistenceFlushFailure,
} from "../../app/persistenceErrors";
import type { PersistenceFlushResult } from "../../app/reloadBlockers";
import { getTabId } from "../../app/tabIdentity";
import { createId } from "../../shared/createId";
import { checkpointMessage } from "./checkpoint";
import { copyConversation, createConversation, sortConversations } from "./reducer";
import { DEFAULT_MODEL, migrateLegacyConversation, migrateLegacyMessage } from "./migration";
import type { Conversation, PersistedConversationState } from "./types";

export interface ConversationCheckpointV2 {
  schemaVersion: 2;
  generation: number;
  savedAt: number;
  currentConversationId: string | null;
  conversations: Conversation[];
}

export interface ConversationCheckpointV3 {
  schemaVersion: 3;
  conversationId: string;
  revision: string;
  parentRevision: string | null;
  writerId: string;
  savedAt: number;
  digest: string;
  conversation: Conversation;
}

/** V3 head 键值：指向当前快照 revision，并保留 parentRevision 作为回退。 */
interface ConversationHeadV3 {
  revision: string;
  parentRevision: string | null;
  writerId: string;
  savedAt: number;
  digest: string;
}

/**
 * V3 冲突指针（`session.v3.conflict.<cid>`）：本地分支被拒绝推进 head 时，
 * 指向已核验落盘的冲突分支快照。每个会话至多一个未解决冲突；解决（查看
 * 最新 / 保留副本）后清除，分支快照随 GC 回收。
 */
export interface ConversationConflictPointer {
  revision: string;
  baseRevision: string | null;
  sharedRevision: string;
  writerId: string;
  savedAt: number;
}

/** 一次成功提交（推进 head 或写入冲突分支）的带外通知，用于跨标签页广播。 */
export interface ConversationCommitNotice {
  conversationId: string;
  revision: string;
  writerId: string;
  savedAt: number;
}

/** 冲突信号：冲突分支已耐久写入（数据安全），但共享 head 由其他标签页持有。 */
export interface ConversationConflictSignal extends ConversationCommitNotice {
  title: string;
  baseRevision: string | null;
  sharedRevision: string;
}

export interface ConversationDeleteNotice {
  conversationId: string;
  writerId: string;
}

/**
 * 删除 tombstone（`session.v3.tombstone.<cid>`）：先于 UI 移除耐久落盘，
 * deletedRevision 形如 "<headSeq+1>.<tabId>"；任何过期 base 的提交一律拒绝。
 */
export interface ConversationTombstone {
  conversationId: string;
  deletedAt: number;
  deletedRevision: string;
  parentRevision: string | null;
  writerId: string;
}

/** tombstone 保留窗口 / 数量上限 / 标签页租约过期时间（租约超过该窗口未触碰即视为不活跃）。 */
export const CONVERSATION_TOMBSTONE_LIMITS = {
  retentionMs: 30 * 24 * 60 * 60 * 1000,
  maxCount: 50,
  tabLeaseStaleMs: 5 * 60 * 1000,
} as const;

/** 提交被 tombstone 拒绝后，本地内容物化为新 id 恢复副本（已作为自己的分片提交）的带外通知。 */
export interface ConversationRecoverySignal {
  conversationId: string;
  copy: Conversation;
}

export interface SaveConversationOptions {
  onCommit?: (notice: ConversationCommitNotice) => void;
  onConflict?: (signal: ConversationConflictSignal) => void;
  onDelete?: (notice: ConversationDeleteNotice) => void;
  onRecovery?: (signal: ConversationRecoverySignal) => void;
}

export interface LockRequestOptions {
  mode: "exclusive" | "shared";
}

/** Web Locks API 的最小结构子集，测试可注入互斥锁实现。 */
export interface LocksLike {
  request<T>(name: string, options: LockRequestOptions, callback: () => T | Promise<T>): Promise<T>;
}

export const CONVERSATION_CHECKPOINT_LOCK_NAME = "deepseek-conversation-checkpoint";

/** 远端提交对账结果：reload = 干净副本已换成共享 head；stale = 本地脏，下次提交走冲突/恢复路径；deleted = 远端已删除且本地干净，调用方移除 state。 */
export type ReconcileRemoteOutcome =
  | { kind: "reload"; conversation: Conversation }
  | { kind: "stale" }
  | { kind: "deleted" }
  | { kind: "noop" };

/**
 * 会话持久化适配器。每个标签页一个实例：本地 base（`lastCommittedRevision`）
 * 与脏检测（`lastCommitted` 对象 identity）都是每标签页状态。模块级导出
 * 函数委托给一个共享默认实例（测试与遗留调用方），Controller 持有自己的实例。
 */
export interface ConversationPersistenceAdapter {
  load(storage?: StorageLike | null, session?: StorageLike | null): PersistedConversationState;
  save(
    state: PersistedConversationState,
    storage?: StorageLike | null,
    session?: StorageLike | null,
    options?: SaveConversationOptions,
  ): PersistenceFlushResult;
  /**
   * 仲裁提交：可用时在排他 Web Lock 临界区内执行与 save 完全相同的检查；
   * 锁缺失（或锁子系统自身报错）时退化为无锁提交——无锁路径做同样的
   * "重读共享 head + 比较 base"，并发冲突退化为冲突分支，绝不静默覆盖。
   * `getState` 在临界区内调用，保证拿到的是最新 state。
   */
  saveArbitrated(
    getState: () => PersistedConversationState,
    storage?: StorageLike | null,
    session?: StorageLike | null,
    options?: SaveConversationOptions,
  ): Promise<PersistenceFlushResult>;
  /** 处理远端提交/删除广播：本地干净则换入共享 head（reload）或按删除移除（deleted）；本地脏则保持（stale）。 */
  reconcileRemoteCommit(
    conversationId: string,
    local: Conversation | undefined,
    storage?: StorageLike | null,
  ): ReconcileRemoteOutcome;
  /** 删除会话：先在仲裁临界区耐久提交 tombstone，成功才由调用方移除 UI 状态。 */
  deleteConversationArbitrated(
    conversationId: string,
    storage?: StorageLike | null,
    session?: StorageLike | null,
    options?: SaveConversationOptions,
  ): Promise<PersistenceFlushResult>;
  /** 触碰（active=true）或移除（active=false）本标签页租约，供 tombstone GC 判断活跃标签页。 */
  setTabLease(active: boolean, storage?: StorageLike | null, session?: StorageLike | null): void;
  readSharedConversation(
    conversationId: string,
    storage?: StorageLike | null,
  ): { conversation: Conversation; revision: string } | null;
  readConflictBranch(
    conversationId: string,
    storage?: StorageLike | null,
  ): { pointer: ConversationConflictPointer; conversation: Conversation } | null;
  clearConflict(conversationId: string, storage?: StorageLike | null): void;
  /** 把换入的远端会话登记为已提交（identity + base），避免紧接着的 flush 重写它。 */
  adoptRemoteConversation(conversationId: string, conversation: Conversation, revision: string): void;
  reset(): void;
}

export const conversationStorageKeys = {
  conversations: "deepseek-infra.conversations",
  currentConversation: "deepseek-infra.current-conversation",
  legacyMessages: "deepseek-infra.messages",
  sessionHead: "deepseek-infra.session.v2.head",
  currentConversationV3: "deepseek-infra.current-conversation.v3",
  v3HeadPrefix: "deepseek-infra.session.v3.head.",
  v3SnapshotPrefix: "deepseek-infra.session.v3.snapshot.",
  v3ConflictPrefix: "deepseek-infra.session.v3.conflict.",
  v3TombstonePrefix: "deepseek-infra.session.v3.tombstone.",
  v3TabPrefix: "deepseek-infra.session.v3.tab.",
} as const;

const V2_SNAPSHOT_PREFIX = "deepseek-infra.session.v2.snapshot.";

export function sessionSnapshotKey(generation: number): string {
  return `${V2_SNAPSHOT_PREFIX}${generation}`;
}

export function sessionHeadKeyV3(conversationId: string): string {
  return `${conversationStorageKeys.v3HeadPrefix}${conversationId}`;
}

export function sessionSnapshotKeyV3(conversationId: string, revision: string): string {
  return `${conversationStorageKeys.v3SnapshotPrefix}${conversationId}.${revision}`;
}

export function sessionConflictKeyV3(conversationId: string): string {
  return `${conversationStorageKeys.v3ConflictPrefix}${conversationId}`;
}

export function sessionTombstoneKeyV3(conversationId: string): string {
  return `${conversationStorageKeys.v3TombstonePrefix}${conversationId}`;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
  length?: number;
  key?(index: number): string | null;
}

function browserStorage(): StorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function browserSessionStorage(): StorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function safeGetItem(storage: StorageLike, key: string): string | null {
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

/** 支持 length/key 的存储可做前缀扫描；不支持时返回 null，调用方静默跳过枚举型逻辑。 */
function enumerateKeysWithPrefix(storage: StorageLike, prefix: string): string[] | null {
  if (typeof storage.length !== "number" || typeof storage.key !== "function") return null;
  const keys: string[] = [];
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key && key.startsWith(prefix)) keys.push(key);
    }
  } catch {
    return null;
  }
  return keys;
}

/** cyrb53：同步、无依赖的哈希，输出 16 位十六进制摘要（用于完整性核验，非加密用途）。 */
export function checkpointDigest(payload: string): string {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let index = 0; index < payload.length; index += 1) {
    const code = payload.charCodeAt(index);
    h1 = Math.imul(h1 ^ code, 2654435761);
    h2 = Math.imul(h2 ^ code, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return `${(h2 >>> 0).toString(16).padStart(8, "0")}${(h1 >>> 0).toString(16).padStart(8, "0")}`;
}

/** revision 形如 `<seq>.<tabId>`；解析失败一律按 0（无父代）处理。 */
function revisionSeq(revision: string | null): number {
  if (!revision) return 0;
  const dot = revision.indexOf(".");
  const seq = Number.parseInt(dot < 0 ? revision : revision.slice(0, dot), 10);
  return Number.isFinite(seq) && seq > 0 ? seq : 0;
}

function parseArray(raw: string | null): unknown[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/** 读取 head 指针指向的 generation；缺失或非法一律按 0（无 V2 checkpoint）处理。 */
function readHeadGeneration(storage: StorageLike): number {
  const raw = safeGetItem(storage, conversationStorageKeys.sessionHead);
  if (!raw) return 0;
  const generation = Number.parseInt(raw, 10);
  return Number.isFinite(generation) && generation > 0 ? generation : 0;
}

function normalizeConversations(value: unknown[]): Conversation[] {
  return value
    .map(migrateLegacyConversation)
    .filter((conversation): conversation is NonNullable<typeof conversation> => Boolean(conversation))
    // 防御性 checkpoint：旧版本写入的"假进行中"消息在加载时同样诚实恢复。
    .map((conversation) => ({ ...conversation, messages: conversation.messages.map(checkpointMessage) }));
}

function selectCurrentConversationId(
  conversations: readonly Conversation[],
  requestedId: string | null,
): string | null {
  return conversations.some((conversation) => conversation.id === requestedId)
    ? requestedId
    : conversations[0]?.id ?? null;
}

/**
 * 读取指定 generation 的 V2 快照。解析、版本、形状任何一步损坏都返回 null，
 * 由调用方回退到上一份 generation 或 legacy 键——绝不返回半个 checkpoint。
 */
function loadCheckpoint(storage: StorageLike, generation: number): PersistedConversationState | null {
  const raw = safeGetItem(storage, sessionSnapshotKey(generation));
  if (!raw) return null;
  let checkpoint: Partial<ConversationCheckpointV2> | null;
  try {
    checkpoint = JSON.parse(raw) as Partial<ConversationCheckpointV2> | null;
  } catch {
    return null;
  }
  if (!checkpoint || typeof checkpoint !== "object") return null;
  if (checkpoint.schemaVersion !== 2 || !Array.isArray(checkpoint.conversations)) return null;
  // 与 legacy 路径共用同一套迁移/校验，Conversation 形状保证完全一致；
  // currentConversationId 与 conversations 始终来自同一份 generation。
  const conversations = sortConversations(normalizeConversations(checkpoint.conversations));
  const requestedId = typeof checkpoint.currentConversationId === "string" ? checkpoint.currentConversationId : null;
  return { schemaVersion: 1, currentConversationId: selectCurrentConversationId(conversations, requestedId), conversations };
}

function parseV3Head(raw: string | null): ConversationHeadV3 | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const head = parsed as Partial<ConversationHeadV3>;
    if (typeof head.revision !== "string" || !head.revision) return null;
    return {
      revision: head.revision,
      parentRevision: typeof head.parentRevision === "string" && head.parentRevision ? head.parentRevision : null,
      writerId: typeof head.writerId === "string" ? head.writerId : "",
      savedAt: typeof head.savedAt === "number" ? head.savedAt : 0,
      digest: typeof head.digest === "string" ? head.digest : "",
    };
  } catch {
    return null;
  }
}

/** 解析 `session.v3.conflict.<cid>` 指针；任何字段非法都返回 null（按无冲突处理）。 */
export function parseV3ConflictPointer(raw: string | null): ConversationConflictPointer | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const pointer = parsed as Partial<ConversationConflictPointer>;
    if (typeof pointer.revision !== "string" || !pointer.revision) return null;
    if (typeof pointer.sharedRevision !== "string" || !pointer.sharedRevision) return null;
    return {
      revision: pointer.revision,
      baseRevision: typeof pointer.baseRevision === "string" && pointer.baseRevision ? pointer.baseRevision : null,
      sharedRevision: pointer.sharedRevision,
      writerId: typeof pointer.writerId === "string" ? pointer.writerId : "",
      savedAt: typeof pointer.savedAt === "number" ? pointer.savedAt : 0,
    };
  } catch {
    return null;
  }
}

/** 读取 JSON 对象上的数值字段（tombstone.deletedAt / 租约 lastSeen）；损坏一律按 0（保守保留）。 */
function readJsonNumber(raw: string | null, field: string): number {
  if (!raw) return 0;
  try {
    const value = (JSON.parse(raw) as Record<string, unknown> | null)?.[field];
    return typeof value === "number" ? value : 0;
  } catch {
    return 0;
  }
}

/** 触碰本标签页租约（best-effort）：每次提交与回到前台时刷新。 */
function writeTabLease(storage: StorageLike, tabId: string): void {
  const now = Date.now();
  try {
    storage.setItem(`${conversationStorageKeys.v3TabPrefix}${tabId}`, JSON.stringify({ tabId, firstSeen: now, lastSeen: now }));
  } catch {
    // 租约只影响 tombstone GC 的保守度，写入失败无害。
  }
}

/**
 * 读取指定 revision 的 V3 会话快照。版本、归属、revision、digest 任何一步
 * 不一致都返回 null，由调用方回退 parentRevision——绝不返回半个分片。
 */
function loadV3SnapshotConversation(storage: StorageLike, conversationId: string, revision: string): Conversation | null {
  const raw = safeGetItem(storage, sessionSnapshotKeyV3(conversationId, revision));
  if (!raw) return null;
  let checkpoint: Partial<ConversationCheckpointV3> | null;
  try {
    checkpoint = JSON.parse(raw) as Partial<ConversationCheckpointV3> | null;
  } catch {
    return null;
  }
  if (!checkpoint || typeof checkpoint !== "object") return null;
  if (checkpoint.schemaVersion !== 3) return null;
  if (checkpoint.conversationId !== conversationId || checkpoint.revision !== revision) return null;
  let payload: string;
  try {
    payload = JSON.stringify(checkpoint.conversation);
  } catch {
    return null;
  }
  // digest 覆盖序列化后的会话负载，任何字节级损坏都视为整份快照不可用。
  if (typeof checkpoint.digest !== "string" || !checkpoint.digest || checkpointDigest(payload) !== checkpoint.digest) {
    return null;
  }
  const conversation = migrateLegacyConversation(checkpoint.conversation);
  if (!conversation) return null;
  return { ...conversation, messages: conversation.messages.map(checkpointMessage) };
}

interface V3LoadedConversation {
  conversation: Conversation;
  headRevision: string;
}

/**
 * 枚举所有 V3 head，逐会话回读：head.revision 快照优先，损坏/缺失时回退
 * parentRevision。遇到 tombstone 的会话直接跳过（写入方在后续提交落地）。
 * 存储不支持枚举时返回 null，调用方继续走 V2 / legacy 读取链。
 */
function loadV3Conversations(storage: StorageLike): V3LoadedConversation[] | null {
  const headKeys = enumerateKeysWithPrefix(storage, conversationStorageKeys.v3HeadPrefix);
  if (!headKeys?.length) return null;
  const conversations: V3LoadedConversation[] = [];
  for (const headKey of headKeys) {
    const conversationId = headKey.slice(conversationStorageKeys.v3HeadPrefix.length);
    if (!conversationId) continue;
    if (safeGetItem(storage, sessionTombstoneKeyV3(conversationId)) !== null) continue;
    const head = parseV3Head(safeGetItem(storage, headKey));
    if (!head) continue;
    const conversation = loadV3SnapshotConversation(storage, conversationId, head.revision)
      ?? (head.parentRevision ? loadV3SnapshotConversation(storage, conversationId, head.parentRevision) : null);
    if (conversation) conversations.push({ conversation, headRevision: head.revision });
  }
  return conversations.length ? conversations : null;
}

function readTabSelection(session: StorageLike | null): string | null {
  if (!session) return null;
  try {
    return session.getItem(conversationStorageKeys.currentConversationV3);
  } catch {
    return null;
  }
}

function writeTabSelection(session: StorageLike | null, conversationId: string | null): void {
  if (!session) return;
  try {
    if (conversationId) session.setItem(conversationStorageKeys.currentConversationV3, conversationId);
    else session.removeItem(conversationStorageKeys.currentConversationV3);
  } catch {
    // 选中态只影响本标签页，sessionStorage 不可用时静默降级。
  }
}

/**
 * 解析本标签页的当前会话选中：迁移来源（V2 / legacy checkpoint 的
 * currentConversationId）优先，否则保留本标签页已存选中；始终对已加载
 * 会话校验，非法则回退到第一个会话，并把结果写回 sessionStorage。
 */
function resolveTabSelection(
  session: StorageLike | null,
  conversations: readonly Conversation[],
  migrateFrom: string | null,
): string | null {
  const migrated = migrateFrom && conversations.some((conversation) => conversation.id === migrateFrom) ? migrateFrom : null;
  const candidate = migrated ?? readTabSelection(session);
  const selected = selectCurrentConversationId(conversations, candidate);
  if (selected) writeTabSelection(session, selected);
  return selected;
}

function normalizeConversationForCommit(conversation: Conversation): Conversation {
  return {
    ...conversation,
    messages: conversation.messages.slice(-80).map(checkpointMessage),
  };
}

/** 估算单个会话序列化后的 UTF-8 字节数，用于分片失败结果里的负载提示。 */
export function estimateConversationBytes(conversation: Conversation): number {
  try {
    return new TextEncoder().encode(JSON.stringify(normalizeConversationForCommit(conversation))).length;
  } catch {
    return 0;
  }
}

function withSizeHint(failure: PersistenceFlushFailure, bytes: number): PersistenceFlushFailure {
  return { ...failure, message: `${failure.message} (~${bytes} bytes)` };
}

function shardFailure(failure: PersistenceFlushFailure, conversationId: string, bytes: number): PersistenceFlushFailure {
  return withSizeHint({ ...failure, message: `会话 ${conversationId}：${failure.message}` }, bytes);
}

/**
 * 冲突分支写入/核验/指针落盘失败：本地修改此刻只存在于内存，按
 * `write-conflict` 失败上报（数据面临风险，进入健康/横幅链路）。
 */
function conflictFailure(reason: string, conversationId: string, bytes: number): PersistenceFlushFailure {
  return withSizeHint({ ok: false, code: "write-conflict", message: `会话 ${conversationId}：冲突分支${reason}` }, bytes);
}

function collectStaleSnapshots(storage: StorageLike, conversationId: string, keep: ReadonlySet<string>, budget: number): void {
  const prefix = sessionSnapshotKeyV3(conversationId, "");
  const keys = enumerateKeysWithPrefix(storage, prefix);
  if (!keys) return; // 存储不支持枚举时静默跳过。
  let remaining = budget;
  for (const key of keys) {
    if (remaining <= 0) return;
    if (keep.has(key.slice(prefix.length))) continue;
    try {
      storage.removeItem(key);
      remaining -= 1;
    } catch {
      // 清理失败无害：残留快照不被 head 引用，空闲 GC 会再次尝试。
    }
  }
}

/** 解析 `session.v3.snapshot.<conversationId>.<seq>.<tabId>`；形状不符返回 null。 */
function parseSnapshotKey(key: string): { conversationId: string; revision: string } | null {
  const rest = key.slice(conversationStorageKeys.v3SnapshotPrefix.length);
  const parts = rest.split(".");
  if (parts.length < 3) return null;
  const revision = parts.slice(-2).join(".");
  const conversationId = parts.slice(0, -2).join(".");
  return conversationId && revisionSeq(revision) > 0 ? { conversationId, revision } : null;
}

/**
 * 空闲孤儿 GC：扫描所有 V3 快照，删除不被所属会话 head / parentRevision /
 * 冲突指针引用的快照（崩溃/失败写入的残留），单次最多 budget 份，返回删除数。
 * 同一份预算内继续回收 tombstone：仅当每个未过期标签页租约的 lastSeen 都晚于
 * tombstone.deletedAt（所有活跃标签页 provably 见过删除），且已超出保留窗口
 * 或数量上限（最旧先回收）时才删除；存储不支持枚举时全部保守保留。
 */
export function runIdleCheckpointGc(storage: StorageLike, budget = 4): number {
  const snapshotKeys = enumerateKeysWithPrefix(storage, conversationStorageKeys.v3SnapshotPrefix);
  if (!snapshotKeys) return 0;
  const keepByConversation = new Map<string, Set<string>>();
  const keepSetFor = (conversationId: string): Set<string> => {
    let keep = keepByConversation.get(conversationId);
    if (!keep) {
      keep = new Set<string>();
      const head = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)));
      if (head) {
        keep.add(head.revision);
        if (head.parentRevision) keep.add(head.parentRevision);
      }
      // 未解决冲突指向的分支快照在解决前受保护，与 current/previous 同规。
      const conflict = parseV3ConflictPointer(safeGetItem(storage, sessionConflictKeyV3(conversationId)));
      if (conflict) keep.add(conflict.revision);
      keepByConversation.set(conversationId, keep);
    }
    return keep;
  };
  let removed = 0;
  for (const key of snapshotKeys) {
    if (removed >= budget) break;
    const parsed = parseSnapshotKey(key);
    if (!parsed) continue;
    if (keepSetFor(parsed.conversationId).has(parsed.revision)) continue;
    try {
      storage.removeItem(key);
      removed += 1;
    } catch {
      // 单次失败跳过即可，下一轮空闲 GC 会重试。
    }
  }
  const tombstoneKeys = enumerateKeysWithPrefix(storage, conversationStorageKeys.v3TombstonePrefix);
  if (!tombstoneKeys?.length) return removed;
  const { retentionMs, maxCount, tabLeaseStaleMs } = CONVERSATION_TOMBSTONE_LIMITS;
  const now = Date.now();
  const entries: { key: string; deletedAt: number }[] = [];
  for (const key of tombstoneKeys) {
    const deletedAt = readJsonNumber(safeGetItem(storage, key), "deletedAt");
    if (deletedAt) entries.push({ key, deletedAt });
  }
  entries.sort((left, right) => left.deletedAt - right.deletedAt);
  let live: number[] | null = null;
  for (let index = 0; index < entries.length; index += 1) {
    if (removed >= budget) break;
    const entry = entries[index] as { key: string; deletedAt: number };
    if (index >= entries.length - maxCount && now - entry.deletedAt <= retentionMs) continue;
    live ??= (enumerateKeysWithPrefix(storage, conversationStorageKeys.v3TabPrefix) ?? [])
      .map((key) => readJsonNumber(safeGetItem(storage, key), "lastSeen"))
      .filter((lastSeen) => now - lastSeen < tabLeaseStaleMs);
    if (!live.every((lastSeen) => lastSeen > entry.deletedAt)) continue;
    try {
      storage.removeItem(entry.key);
      removed += 1;
    } catch {
      // 下一轮空闲 GC 会重试。
    }
  }
  return removed;
}

function detectNavigatorLocks(): LocksLike | null {
  const nav = (globalThis as { navigator?: { locks?: Partial<LocksLike> } }).navigator;
  const locks = nav?.locks;
  const request = locks?.request;
  if (!locks || typeof request !== "function") return null;
  const bound = request.bind(locks) as LocksLike["request"];
  return {
    request: (name, options, callback) => bound(name, options, callback),
  };
}

export interface ConversationPersistenceAdapterOptions {
  /** 显式注入锁实现（测试互斥锁）；null 强制无锁路径；缺省则运行时探测 navigator.locks。 */
  locks?: LocksLike | null;
}

export function createConversationPersistenceAdapter(
  adapterOptions: ConversationPersistenceAdapterOptions = {},
): ConversationPersistenceAdapter {
  /**
   * 持久化适配器状态。chat reducer 只替换发生变化的会话对象（其余保持对象
   * identity），因此 identity 对比即可圈出脏分片：一次会话变更只序列化该会话。
   */
  const lastCommitted = new Map<string, Conversation>();
  const lastCommittedRevision = new Map<string, string>();
  /** Controller 已请求但尚未耐久提交的删除（pagehide 同步 flush 会先提交它们）。 */
  const pendingTombstones = new Set<string>();
  /** 本标签页已知被删除的会话：提交一律跳过（删除方自身状态 / 已物化过恢复副本）。 */
  const tombstonedRefused = new Set<string>();
  let lastHeadRevision: string | null = null;
  let idleGcScheduled = false;

  function load(storage: StorageLike | null = browserStorage(), session: StorageLike | null = browserSessionStorage()): PersistedConversationState {
    if (!storage) return { schemaVersion: 1, currentConversationId: null, conversations: [] };
    // V3 分片优先；其后依次为 V2 journal（迁移读取器，保持原逻辑）与 legacy 键。
    const sharded = loadV3Conversations(storage);
    if (sharded) {
      const conversations = sortConversations(sharded.map((entry) => entry.conversation));
      const headRevisionById = new Map(sharded.map((entry) => [entry.conversation.id, entry.headRevision]));
      // V3 加载的分片内容与共享 head 一致：按"已提交"登记（identity + base），
      // 第二个打开的标签页因此不会重写共享 head，更不会在仲裁下制造伪冲突。
      // 只对进入 state 的会话登记（sortConversations 有数量上限），避免把被
      // 截断的会话误判为"本地已删除"。V2 / legacy 加载不登记——那些内容尚未
      // 落入 V3，必须按脏提交完成迁移。
      for (const conversation of conversations) {
        const headRevision = headRevisionById.get(conversation.id);
        if (!headRevision) continue;
        lastCommitted.set(conversation.id, conversation);
        lastCommittedRevision.set(conversation.id, headRevision);
      }
      return { schemaVersion: 1, currentConversationId: resolveTabSelection(session, conversations, null), conversations };
    }
    const head = readHeadGeneration(storage);
    for (const generation of [head, head - 1]) {
      if (generation < 1) continue;
      const checkpoint = loadCheckpoint(storage, generation);
      if (checkpoint) {
        return {
          schemaVersion: 1,
          currentConversationId: resolveTabSelection(session, checkpoint.conversations, checkpoint.currentConversationId),
          conversations: checkpoint.conversations,
        };
      }
    }
    let conversations = normalizeConversations(parseArray(safeGetItem(storage, conversationStorageKeys.conversations)));

    if (!conversations.length) {
      const messages = parseArray(safeGetItem(storage, conversationStorageKeys.legacyMessages))
        .map(migrateLegacyMessage)
        .filter((message): message is ChatMessage => Boolean(message));
      if (messages.length) {
        conversations = [createConversation(createId("legacy-conversation"), messages.map(checkpointMessage), DEFAULT_MODEL, true)];
      }
    }

    conversations = sortConversations(conversations);
    const requestedId = safeGetItem(storage, conversationStorageKeys.currentConversation);
    return { schemaVersion: 1, currentConversationId: resolveTabSelection(session, conversations, requestedId), conversations };
  }

  type ShardCommitOutcome =
    | { ok: true; kind: "head"; revision: string; savedAt: number }
    | {
        ok: true;
        kind: "conflict";
        revision: string;
        savedAt: number;
        baseRevision: string | null;
        sharedRevision: string;
      }
    | PersistenceFlushFailure;

  /**
   * 提交单个会话分片（跨标签页仲裁版）：先重读共享 head，与适配器本地 base
   * （本标签页上次提交推进到的 head revision）比较——
   * - base 与共享 head 一致（或 head 缺失，按首次写入）→ 既有协议：snapshot
   *   写入 → 回读核验 → head 推进；
   * - 不一致 → 绝不覆盖：本地分支写为冲突快照（revision = 共享 head 序号 + 1
   *   并内嵌本标签页 tabId，永与胜方 head revision 不撞；parentRevision =
   *   本地 base），回读核验后才写入/替换冲突指针（指针永远指向已确认落盘的
   *   分支，每个会话至多一个未解决冲突）。
   * 无锁路径执行完全相同的"重读 + 比较"：revision 内嵌 tabId，其他标签页
   * 写入的 head 必然被识别为兄弟分支，并发提交退化为冲突副本而不是静默的
   * last-write-wins。任何路径都不抛异常。
   */
  function commitConversationShard(storage: StorageLike, conversation: Conversation, tabId: string): ShardCommitOutcome {
    const bytes = estimateConversationBytes(conversation);
    let headRaw: string | null;
    try {
      headRaw = storage.getItem(sessionHeadKeyV3(conversation.id));
    } catch (error) {
      return shardFailure(classifyStorageError(error), conversation.id, bytes);
    }
    const head = parseV3Head(headRaw);
    const sharedRevision = head?.revision ?? null;
    const localBase = lastCommittedRevision.get(conversation.id) ?? null;
    const conflicted = head !== null && sharedRevision !== localBase;
    const fail = (error: unknown): PersistenceFlushFailure => conflicted
      ? conflictFailure(`写入失败：${classifyStorageError(error).message}`, conversation.id, bytes)
      : shardFailure(classifyStorageError(error), conversation.id, bytes);
    const revision = `${revisionSeq(sharedRevision) + 1}.${tabId}`;
    const parentRevision = conflicted ? localBase : sharedRevision;
    let serialized: string;
    let digest: string;
    let savedAt = 0;
    try {
      const normalized = normalizeConversationForCommit(conversation);
      const payload = JSON.stringify(normalized);
      digest = checkpointDigest(payload);
      savedAt = Date.now();
      const checkpoint: ConversationCheckpointV3 = {
        schemaVersion: 3,
        conversationId: conversation.id,
        revision,
        parentRevision,
        writerId: tabId,
        savedAt,
        digest,
        conversation: normalized,
      };
      serialized = JSON.stringify(checkpoint);
    } catch (error) {
      return fail(error);
    }
    const snapshotKey = sessionSnapshotKeyV3(conversation.id, revision);
    try {
      storage.setItem(snapshotKey, serialized);
      // 回读核验：字节级比对刚写入的快照，任何损坏都视为整份不可用。
      if (storage.getItem(snapshotKey) !== serialized) {
        return conflicted
          ? conflictFailure("写入后回读核验失败", conversation.id, bytes)
          : shardFailure(verificationFailure("快照写入后回读核验失败"), conversation.id, bytes);
      }
    } catch (error) {
      return fail(error);
    }
    writeTabLease(storage, tabId);
    if (conflicted && head) {
      // 分支快照已确认落盘，此刻才允许写入/替换冲突指针；上一份被替换掉的
      // 分支失去指针保护，由保留/空闲 GC 回收。
      const pointer: ConversationConflictPointer = {
        revision,
        baseRevision: localBase,
        sharedRevision: head.revision,
        writerId: tabId,
        savedAt,
      };
      try {
        storage.setItem(sessionConflictKeyV3(conversation.id), JSON.stringify(pointer));
      } catch (error) {
        return fail(error);
      }
      return { ok: true, kind: "conflict", revision, savedAt, baseRevision: localBase, sharedRevision: head.revision };
    }
    try {
      storage.setItem(sessionHeadKeyV3(conversation.id), JSON.stringify({
        revision,
        parentRevision,
        writerId: tabId,
        savedAt,
        digest,
      } satisfies ConversationHeadV3));
    } catch (error) {
      return shardFailure(classifyStorageError(error), conversation.id, bytes);
    }
    // 保留式 GC：有界 O(1)——最多删除 2 份既非当前、也非 parentRevision、
    // 也非冲突指针引用分支的旧快照。
    const keep = new Set(parentRevision ? [revision, parentRevision] : [revision]);
    const conflict = parseV3ConflictPointer(safeGetItem(storage, sessionConflictKeyV3(conversation.id)));
    if (conflict) keep.add(conflict.revision);
    collectStaleSnapshots(storage, conversation.id, keep, 2);
    return { ok: true, kind: "head", revision, savedAt };
  }

  /**
   * 提交删除 tombstone：写 tombstone 键 → 回读核验 → 有界删除 head / 冲突指针 /
   * ≤2 份快照。已存在 tombstone 时幂等跳过重写（不重复发删除通知）。成功后本
   * 标签页不再为该 cid 提交（含删除方自身的残留状态），写 / 核验失败返回映射
   * 失败结果，head 与已提交映射原样保留，会话可继续提交或重试删除。
   */
  function commitTombstone(
    storage: StorageLike,
    conversationId: string,
    tabId: string,
    options?: SaveConversationOptions,
  ): PersistenceFlushFailure | null {
    const key = sessionTombstoneKeyV3(conversationId);
    const fresh = safeGetItem(storage, key) === null;
    if (fresh) {
      const parentRevision = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)))?.revision
        ?? lastCommittedRevision.get(conversationId)
        ?? null;
      const serialized = JSON.stringify({
        conversationId,
        deletedAt: Date.now(),
        deletedRevision: `${revisionSeq(parentRevision) + 1}.${tabId}`,
        parentRevision,
        writerId: tabId,
      } satisfies ConversationTombstone);
      try {
        storage.setItem(key, serialized);
        if (storage.getItem(key) !== serialized) {
          return shardFailure(verificationFailure("tombstone 核验失败"), conversationId, 0);
        }
      } catch (error) {
        return shardFailure(classifyStorageError(error), conversationId, 0);
      }
      writeTabLease(storage, tabId);
    }
    deleteConversationShard(storage, conversationId);
    tombstonedRefused.add(conversationId);
    // 删除通知在分片清理之后才发出：接收端对账时 head 必已消失。
    if (fresh) options?.onDelete?.({ conversationId, writerId: tabId });
    return null;
  }

  /**
   * 删除 tombstone 之后清理会话分片：head + 冲突指针 + 至多 2 份快照。
   * 全部删除成功才移除已提交映射；失败则保留映射（残留键不被 tombstone
   * 遮挡的加载路径引用，空闲 GC 与后续删除会再次尝试）。
   */
  function deleteConversationShard(storage: StorageLike, conversationId: string): void {
    let failed = false;
    const drop = (key: string): void => {
      try {
        storage.removeItem(key);
      } catch {
        failed = true;
      }
    };
    const knownRevision = lastCommittedRevision.get(conversationId);
    drop(sessionHeadKeyV3(conversationId));
    drop(sessionConflictKeyV3(conversationId));
    if (knownRevision) drop(sessionSnapshotKeyV3(conversationId, knownRevision));
    let removed = knownRevision ? 1 : 0;
    for (const key of enumerateKeysWithPrefix(storage, sessionSnapshotKeyV3(conversationId, "")) ?? []) {
      if (removed >= 2) break;
      if (key === sessionSnapshotKeyV3(conversationId, knownRevision ?? "")) continue;
      drop(key);
      removed += 1;
    }
    if (!failed) {
      lastCommitted.delete(conversationId);
      lastCommittedRevision.delete(conversationId);
    }
  }

  /**
   * 首个所有脏分片都提交成功的 flush 之后，才删除 V2 head / snapshot 与 legacy
   * conversations / currentConversation 键（此刻它们的内容已核验落入 V3，
   * 与 4.3.5 的"先验证后删除"同规）。legacyMessages 永不删除（与 4.3.5 语义一致）。
   */
  function cleanupMigratedKeys(storage: StorageLike): void {
    const legacyKeys = [
      conversationStorageKeys.sessionHead,
      conversationStorageKeys.conversations,
      conversationStorageKeys.currentConversation,
    ];
    let hasMigratedKeys = false;
    for (const key of legacyKeys) {
      try {
        if (storage.getItem(key) !== null) hasMigratedKeys = true;
      } catch {
        return; // 连读都失败时不做任何删除。
      }
    }
    let v2SnapshotKeys = enumerateKeysWithPrefix(storage, V2_SNAPSHOT_PREFIX);
    if (v2SnapshotKeys === null) {
      // 不支持枚举：V2 最多保留 head 与 head-1 两份，按此兜底。
      const head = readHeadGeneration(storage);
      v2SnapshotKeys = [head, head - 1].filter((generation) => generation >= 1).map(sessionSnapshotKey);
    } else if (v2SnapshotKeys.length) {
      hasMigratedKeys = true;
    }
    if (!hasMigratedKeys) return;
    for (const key of [...legacyKeys, ...v2SnapshotKeys]) {
      try {
        storage.removeItem(key);
      } catch {
        // 残留键只会让旧读取器看到过期数据，V3 提交本身已完成。
      }
    }
  }

  function scheduleIdleCheckpointGc(storage: StorageLike): void {
    if (idleGcScheduled) return;
    idleGcScheduled = true;
    const run = (): void => {
      idleGcScheduled = false;
      try {
        runIdleCheckpointGc(storage);
      } catch {
        // 空闲 GC 永远不允许抛出。
      }
    };
    const requestIdle = (globalThis as { requestIdleCallback?: (callback: () => void) => void }).requestIdleCallback;
    requestIdle?.(run) ?? setTimeout(run, 0);
  }

  /**
   * 以会话为分片提交会话状态：
   *   待提交 tombstone 优先（Controller 请求的删除；pagehide 同步 flush 也在此
   *   以无锁同等检查提交）→ 逐个脏会话仲裁提交（重读共享 head → 一致推进 /
   *   不一致写冲突分支；被 tombstone 拒绝的会话物化为新 id 恢复副本并随本次
   *   flush 提交）→ 有界保留 GC → 全部脏分片成功后清理 V2 / legacy 键 →
   *   调度一次空闲孤儿 GC。删除只经 tombstone 通道（deleteConversationArbitrated
   *   登记待删标记），state 收缩本身绝不删除分片。
   * 当前会话选中只写入本标签页的 sessionStorage，绝不进入 V3 共享键。
   * 冲突分支耐久写入仍算 {ok:true}（数据安全，经 onConflict 带外上报）；
   * 冲突分支写失败返回 write-conflict（数据面临风险）。
   * 任何路径都不抛异常，失败以 PersistenceFlushResult 返回并附估算负载大小。
   */
  function save(
    state: PersistedConversationState,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
    options?: SaveConversationOptions,
  ): PersistenceFlushResult {
    if (!storage) return storageUnavailableFailure(`localStorage 不可用 (~${JSON.stringify(state).length} bytes)`);
    const tabId = getTabId(session);
    writeTabSelection(session, state.currentConversationId);

    // 待提交 tombstone 优先（Controller 请求的删除；pagehide 同步 flush 也在此提交）。
    for (const conversationId of pendingTombstones) {
      const failure = commitTombstone(storage, conversationId, tabId, options);
      if (failure) return failure; // 保留待删标记，下一轮 flush 重试。
      pendingTombstones.delete(conversationId);
    }

    // 工作队列：被 tombstone 拒绝的会话物化为恢复副本后入队，走同一条提交路径。
    const queue = [...state.conversations];
    for (const conversation of queue) {
      if (!conversation.messages.length
        || lastCommitted.get(conversation.id) === conversation
        || tombstonedRefused.has(conversation.id)) continue;
      // tombstone 守卫：本地 base 早于删除（或根本没收到删除通知）的提交一律拒绝，
      // head / 快照绝不重写；cid 移出已提交映射，本地内容以新 id 恢复副本幸存。
      if (safeGetItem(storage, sessionTombstoneKeyV3(conversation.id)) !== null) {
        tombstonedRefused.add(conversation.id);
        lastCommitted.delete(conversation.id);
        lastCommittedRevision.delete(conversation.id);
        const copy = copyConversation(conversation, "（恢复副本）");
        queue.push(copy);
        options?.onRecovery?.({ conversationId: conversation.id, copy });
        continue;
      }
      const outcome = commitConversationShard(storage, conversation, tabId);
      if (!outcome.ok) return outcome;
      // 冲突提交后 base 跟进胜方 head，下一次提交在胜方之上推进。
      lastCommitted.set(conversation.id, conversation);
      lastCommittedRevision.set(conversation.id, outcome.kind === "conflict" ? outcome.sharedRevision : outcome.revision);
      lastHeadRevision = outcome.revision;
      options?.onCommit?.({ conversationId: conversation.id, revision: outcome.revision, writerId: tabId, savedAt: outcome.savedAt });
      if (outcome.kind === "conflict") {
        options?.onConflict?.({
          conversationId: conversation.id,
          title: conversation.title,
          revision: outcome.revision,
          baseRevision: outcome.baseRevision,
          sharedRevision: outcome.sharedRevision,
          writerId: tabId,
          savedAt: outcome.savedAt,
        });
      }
    }

    // 此刻 state 中所有会话都已核验落入 V3，才允许清理 V2 / legacy 键。
    cleanupMigratedKeys(storage);
    scheduleIdleCheckpointGc(storage);
    return lastHeadRevision ? { ok: true, revision: lastHeadRevision } : { ok: true };
  }

  /** 在排他锁内执行 fn；锁缺失或锁子系统自身失败时退化为无锁执行（同等检查在 fn 内）。 */
  async function arbitrate<T>(fn: () => T): Promise<T> {
    const locks = adapterOptions.locks === undefined ? detectNavigatorLocks() : adapterOptions.locks;
    if (!locks) return fn();
    try {
      return await locks.request(CONVERSATION_CHECKPOINT_LOCK_NAME, { mode: "exclusive" }, fn);
    } catch {
      return fn();
    }
  }

  function saveArbitrated(
    getState: () => PersistedConversationState,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
    options?: SaveConversationOptions,
  ): Promise<PersistenceFlushResult> {
    return arbitrate(() => save(getState(), storage, session, options));
  }

  /**
   * Controller 删除入口：先在仲裁临界区耐久提交 tombstone（锁缺失退化无锁同等
   * 检查），成功才由调用方移除 UI 状态；失败即取消待删标记，会话保留可重试。
   * pagehide 同步 flush 可能先于此承诺解决提交同一 tombstone（幂等，通知仅一次）。
   */
  async function deleteConversationArbitrated(
    conversationId: string,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
    options?: SaveConversationOptions,
  ): Promise<PersistenceFlushResult> {
    if (!storage) return storageUnavailableFailure("localStorage 不可用");
    const tabId = getTabId(session);
    pendingTombstones.add(conversationId);
    return arbitrate(() => {
      const failure = commitTombstone(storage, conversationId, tabId, options);
      pendingTombstones.delete(conversationId);
      return failure ?? { ok: true };
    });
  }

  function setTabLease(
    active: boolean,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
  ): void {
    if (active && storage) {
      writeTabLease(storage, getTabId(session));
      return;
    }
    try {
      storage?.removeItem(`${conversationStorageKeys.v3TabPrefix}${getTabId(session)}`);
    } catch {
      // best-effort：未删除的租约过期后自然不再阻挡 GC。
    }
  }

  function reconcileRemoteCommit(
    conversationId: string,
    local: Conversation | undefined,
    storage: StorageLike | null = browserStorage(),
  ): ReconcileRemoteOutcome {
    if (!storage) return { kind: "noop" };
    const head = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)));
    if (head && lastCommittedRevision.get(conversationId) === head.revision) return { kind: "noop" };
    // 本地有未提交修改：保持内容，下次提交走冲突 / 恢复副本路径（绝不静默丢弃）。
    if (local && lastCommitted.get(conversationId) !== local) return { kind: "stale" };
    if (!head) {
      // head 缺失且 tombstone 在案：远端已删除，本地干净 ⇒ 调用方移除 state。
      if (safeGetItem(storage, sessionTombstoneKeyV3(conversationId)) === null) return { kind: "noop" };
      lastCommitted.delete(conversationId);
      lastCommittedRevision.delete(conversationId);
      return { kind: "deleted" };
    }
    const conversation = loadV3SnapshotConversation(storage, conversationId, head.revision)
      ?? (head.parentRevision ? loadV3SnapshotConversation(storage, conversationId, head.parentRevision) : null);
    if (!conversation) return { kind: "noop" };
    lastCommitted.set(conversationId, conversation);
    lastCommittedRevision.set(conversationId, head.revision);
    return { kind: "reload", conversation };
  }

  function readSharedConversation(
    conversationId: string,
    storage: StorageLike | null = browserStorage(),
  ): { conversation: Conversation; revision: string } | null {
    if (!storage) return null;
    const head = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)));
    if (!head) return null;
    const conversation = loadV3SnapshotConversation(storage, conversationId, head.revision)
      ?? (head.parentRevision ? loadV3SnapshotConversation(storage, conversationId, head.parentRevision) : null);
    return conversation ? { conversation, revision: head.revision } : null;
  }

  function readConflictBranch(
    conversationId: string,
    storage: StorageLike | null = browserStorage(),
  ): { pointer: ConversationConflictPointer; conversation: Conversation } | null {
    if (!storage) return null;
    const pointer = parseV3ConflictPointer(safeGetItem(storage, sessionConflictKeyV3(conversationId)));
    if (!pointer) return null;
    const conversation = loadV3SnapshotConversation(storage, conversationId, pointer.revision);
    return conversation ? { pointer, conversation } : null;
  }

  function clearConflict(conversationId: string, storage: StorageLike | null = browserStorage()): void {
    if (!storage) return;
    try {
      storage.removeItem(sessionConflictKeyV3(conversationId));
    } catch {
      // 残留指针只会让分支多受保护一轮 GC，无害。
    }
  }

  function adoptRemoteConversation(conversationId: string, conversation: Conversation, revision: string): void {
    lastCommitted.set(conversationId, conversation);
    lastCommittedRevision.set(conversationId, revision);
  }

  function reset(): void {
    lastCommitted.clear();
    lastCommittedRevision.clear();
    pendingTombstones.clear();
    tombstonedRefused.clear();
    lastHeadRevision = null;
    idleGcScheduled = false;
  }

  return {
    load,
    save,
    saveArbitrated,
    reconcileRemoteCommit,
    deleteConversationArbitrated,
    setTabLease,
    readSharedConversation,
    readConflictBranch,
    clearConflict,
    adoptRemoteConversation,
    reset,
  };
}

const defaultAdapter = createConversationPersistenceAdapter();

export function loadPersistedConversationState(
  storage: StorageLike | null = browserStorage(),
  session: StorageLike | null = browserSessionStorage(),
): PersistedConversationState {
  return defaultAdapter.load(storage, session);
}

export function savePersistedConversationState(
  state: PersistedConversationState,
  storage: StorageLike | null = browserStorage(),
  session: StorageLike | null = browserSessionStorage(),
  options?: SaveConversationOptions,
): PersistenceFlushResult {
  return defaultAdapter.save(state, storage, session, options);
}

/** 通过默认适配器读取冲突分支（冲突快照经 digest 核验；损坏返回 null）。 */
export function readConflictBranch(
  conversationId: string,
  storage: StorageLike | null = browserStorage(),
): { pointer: ConversationConflictPointer; conversation: Conversation } | null {
  return defaultAdapter.readConflictBranch(conversationId, storage);
}

export function resetConversationPersistenceForTests(): void {
  defaultAdapter.reset();
}
