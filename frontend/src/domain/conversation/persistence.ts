import type { ChatMessage } from "../chat/types";
import {
  classifyStorageError,
  storageUnavailableFailure,
  verificationFailure,
  type PersistenceFlushFailure,
} from "../../app/persistenceErrors";
import type { PersistenceFlushResult } from "../../app/reloadBlockers";
import {
  createReplicaIdentity,
  getTabId,
  type ReplicaIdentity,
  type ReplicaIdentityOverrides,
} from "../../app/tabIdentity";
import { createId } from "../../shared/createId";
import { checkpointMessage } from "./checkpoint";
import { compactConversationForStorage, type CheckpointCompaction } from "./compaction";
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
  /** 存储压力降级落盘的压缩记录（级别 + 剥离的预览字节数）；仅压缩重试成功时存在。 */
  compaction?: CheckpointCompaction;
}

/** V3 head 键值：指向当前快照 revision，并保留 parentRevision 作为回退。 */
interface ConversationHeadV3 {
  revision: string;
  parentRevision: string | null;
  writerId: string;
  savedAt: number;
  digest: string;
}

/** Web Locks 不可用时发布的候选提交；快照经 digest 核验后才参与仲裁。 */
interface LockFreeProposal {
  schemaVersion: 1;
  conversationId: string;
  parentRevision: string | null;
  revision: string;
  logicalSequence: number;
  savedAt: number;
  writerSessionId: string;
  digest: string;
}

function parseLockFreeProposal(raw: string | null): LockFreeProposal | null {
  if (!raw) return null;
  try {
    const proposal = JSON.parse(raw) as Partial<LockFreeProposal>;
    if (proposal.schemaVersion !== 1
      || typeof proposal.conversationId !== "string" || !proposal.conversationId
      || typeof proposal.revision !== "string" || !proposal.revision
      || typeof proposal.logicalSequence !== "number"
      || typeof proposal.savedAt !== "number"
      || typeof proposal.writerSessionId !== "string" || !proposal.writerSessionId
      || typeof proposal.digest !== "string" || !proposal.digest) return null;
    return {
      schemaVersion: 1,
      conversationId: proposal.conversationId,
      parentRevision: typeof proposal.parentRevision === "string" && proposal.parentRevision ? proposal.parentRevision : null,
      revision: proposal.revision,
      logicalSequence: proposal.logicalSequence,
      savedAt: proposal.savedAt,
      writerSessionId: proposal.writerSessionId,
      digest: proposal.digest,
    };
  } catch {
    return null;
  }
}

/** 4.3.6 单指针读取兼容层；4.3.7 的真实所有权由 DurableConflictBranch 账本承担。 */
export interface ConversationConflictPointer {
  revision: string;
  baseRevision: string | null;
  sharedRevision: string;
  writerId: string;
  savedAt: number;
}

export type DurableConflictStatus = "pending" | "resolved-copy" | "discarded";

/** 每个失败副本一条不可覆盖的耐久记录。 */
export interface DurableConflictBranch {
  schemaVersion: 1;
  conflictId: string;
  conversationId: string;
  branchRevision: string;
  parentBranchRevision: string | null;
  baseRevision: string | null;
  sharedRevision: string;
  writerSessionId: string;
  createdAt: number;
  updatedAt: number;
  status: DurableConflictStatus;
}

export interface LocalConflictBranchState {
  conversationId: string;
  conflictId: string;
  branchRevision: string;
  baseRevision: string | null;
  sharedRevision: string;
  status: "pending" | "editing-branch" | "materializing-copy" | "discarding";
}

export interface LoadedConversationShard {
  conversation: Conversation;
  advertisedRevision: string;
  loadedRevision: string;
  degraded: boolean;
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
  conflictId: string;
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

/**
 * localStorage 的跨 renderer 操作不是一笔事务。快照写入成功到 Proposal 发布之间
 * 必须留出短暂保护窗口，避免另一个标签页的 GC 把尚未被引用的新快照误判为孤儿。
 */
export const CONVERSATION_CHECKPOINT_ORPHAN_GRACE_MS = 60 * 1000;

/** 提交被 tombstone 拒绝后，本地内容物化为新 id 恢复副本（已作为自己的分片提交）的带外通知。 */
export interface ConversationRecoverySignal {
  conversationId: string;
  copy: Conversation;
}

/** 一次带压缩成功的提交（存储压力降级落盘）的带外信号：压缩级别 + 实际剥离的预览字节数。 */
export interface ConversationCompactionSignal {
  conversationId: string;
  revision: string;
  level: number;
  removedPreviewBytes: number;
}

export interface RecoveryCapsuleEntry {
  conversationId: string;
  baseRevision: string | null;
  digest: string;
  conversation: Conversation;
  compaction?: CheckpointCompaction;
}

/** 页面退出应急胶囊 V2：逐条目摘要 + 整体摘要，写后必须逐字节回读核验。 */
export interface RecoveryCapsule {
  schemaVersion: 2;
  writerSessionId: string;
  sequence: number;
  savedAt: number;
  digest: string;
  entries: RecoveryCapsuleEntry[];
}

/** 胶囊对账后补交落盘的一条会话：previous 是对账前本地已提交对象（identity 比较用）。 */
export interface RecoveryCapsuleCommit {
  conversationId: string;
  conversation: Conversation;
  revision: string;
  previous: Conversation | null;
}

/** 启动胶囊对账结果：补交落盘的会话、物化的恢复副本、未能耐久落盘的失败（胶囊保留）。 */
export interface RecoveryReconcileOutcome {
  committed: RecoveryCapsuleCommit[];
  recovered: Conversation[];
  failed: PersistenceFlushFailure[];
}

export interface SaveConversationOptions {
  onCommit?: (notice: ConversationCommitNotice) => void;
  onConflict?: (signal: ConversationConflictSignal) => void;
  onDelete?: (notice: ConversationDeleteNotice) => void;
  onRecovery?: (signal: ConversationRecoverySignal) => void;
  onCompaction?: (signal: ConversationCompactionSignal) => void;
  /** 生命周期同步 flush 无法取得排他锁；该模式只允许写 Recovery Capsule。 */
  lifecycle?: boolean;
  onWarning?: (failure: PersistenceFlushFailure) => void;
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
  /** 持久化本标签页的当前会话选中（sessionStorage，best-effort）；选中是纯标签页 UI 状态，绝不进入共享键。 */
  persistSelection(conversationId: string | null, session?: StorageLike | null): void;
  /** 删除会话：先在仲裁临界区耐久提交 tombstone，成功才由调用方移除 UI 状态。 */
  deleteConversationArbitrated(
    conversationId: string,
    storage?: StorageLike | null,
    session?: StorageLike | null,
    options?: SaveConversationOptions,
  ): Promise<PersistenceFlushResult>;
  /** 触碰（active=true）或移除（active=false）本标签页租约，供 tombstone GC 判断活跃标签页。 */
  setTabLease(active: boolean, storage?: StorageLike | null, session?: StorageLike | null): void;
  /**
   * 页面退出应急胶囊：以当前仍处于脏状态的会话同步写本标签页胶囊键；脏集合为空
   * （flush 成功）时改为移除胶囊键。绝不推进任何共享 head；写入失败返回失败结果
   * （由调用方经 persistenceHealth 记录），任何路径都不抛异常。
   */
  writeRecoveryCapsule(
    state: PersistedConversationState,
    storage?: StorageLike | null,
    session?: StorageLike | null,
  ): PersistenceFlushFailure | null;
  /**
   * 启动胶囊对账（排他写锁内）：先处理本标签页胶囊（崩溃/杀死但会话恢复），再在
   * 预算内回收属主租约缺失或已过期的孤儿胶囊；活租约胶囊留给属主（BFCache 恢复
   * 时属主经胶囊保鲜规则自行清理）。逐条目——head 缺失（无 tombstone）或
   * head.revision === base ⇒ 作为正常分片提交补交；head 已推进或 cid 已被
   * tombstone 覆盖 ⇒ 以确定性 id `<cid>.recovered.<tabId>` 物化恢复副本。
   * 全部条目耐久处理后删除胶囊键；任一条目失败则保留胶囊（下次启动幂等重试，
   * digest / 副本 head 检查保证重处理收敛而非重复）。
   */
  reconcileRecoveryCapsules(
    storage?: StorageLike | null,
    session?: StorageLike | null,
    options?: SaveConversationOptions,
  ): Promise<RecoveryReconcileOutcome>;
  readSharedConversation(
    conversationId: string,
    storage?: StorageLike | null,
  ): { conversation: Conversation; revision: string } | null;
  readConflictBranch(
    conversationId: string,
    storage?: StorageLike | null,
    conflictId?: string,
  ): { pointer: ConversationConflictPointer; branch: DurableConflictBranch; conversation: Conversation } | null;
  listConflictBranches(conversationId?: string, storage?: StorageLike | null): DurableConflictBranch[];
  clearConflict(conversationId: string, storage?: StorageLike | null, conflictId?: string): void;
  resolveConflictByCopyArbitrated(
    conversationId: string,
    conflictId: string,
    storage?: StorageLike | null,
    session?: StorageLike | null,
    options?: SaveConversationOptions,
  ): Promise<{ ok: true; copy: Conversation } | PersistenceFlushFailure>;
  resolveConflictByReloadArbitrated(
    conversationId: string,
    conflictId: string,
    storage?: StorageLike | null,
  ): Promise<{ ok: true; conversation: Conversation; revision: string } | PersistenceFlushFailure>;
  getReplicaIdentity(session?: StorageLike | null): ReplicaIdentity;
  rotateWriterIdentity(session?: StorageLike | null): ReplicaIdentity;
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
  v3ConflictIndexPrefix: "deepseek-infra.session.v3.conflict-index.",
  v3ProposalPrefix: "deepseek-infra.session.v3.proposal.",
  v3TombstonePrefix: "deepseek-infra.session.v3.tombstone.",
  v3TabPrefix: "deepseek-infra.session.v3.tab.",
  v3RecoveryPrefix: "deepseek-infra.session.v3.recovery.",
  v3RecoveryResolvedPrefix: "deepseek-infra.session.v3.recovery-resolved.",
  v3QuarantinePrefix: "deepseek-infra.session.v3.quarantine.",
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

export function sessionConflictKeyV3(conversationId: string, conflictId?: string): string {
  return `${conversationStorageKeys.v3ConflictPrefix}${conversationId}${conflictId ? `.${conflictId}` : ""}`;
}

export function sessionConflictIndexKeyV3(conversationId: string): string {
  return `${conversationStorageKeys.v3ConflictIndexPrefix}${conversationId}`;
}

export function sessionProposalKeyV3(conversationId: string, parentRevision: string | null, revision: string): string {
  return `${conversationStorageKeys.v3ProposalPrefix}${conversationId}.${parentRevision ?? "root"}.${revision}`;
}

export function sessionTombstoneKeyV3(conversationId: string): string {
  return `${conversationStorageKeys.v3TombstonePrefix}${conversationId}`;
}

export function sessionRecoveryKeyV3(writerSessionId: string): string {
  return `${conversationStorageKeys.v3RecoveryPrefix}${writerSessionId}`;
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

export function parseDurableConflictBranch(raw: string | null): DurableConflictBranch | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const branch = parsed as Partial<DurableConflictBranch>;
    if (branch.schemaVersion !== 1
      || typeof branch.conflictId !== "string" || !branch.conflictId
      || typeof branch.conversationId !== "string" || !branch.conversationId
      || typeof branch.branchRevision !== "string" || !branch.branchRevision
      || typeof branch.sharedRevision !== "string" || !branch.sharedRevision
      || typeof branch.writerSessionId !== "string" || !branch.writerSessionId
      || !["pending", "resolved-copy", "discarded"].includes(branch.status ?? "")) return null;
    return {
      schemaVersion: 1,
      conflictId: branch.conflictId,
      conversationId: branch.conversationId,
      branchRevision: branch.branchRevision,
      parentBranchRevision: typeof branch.parentBranchRevision === "string" && branch.parentBranchRevision
        ? branch.parentBranchRevision
        : null,
      baseRevision: typeof branch.baseRevision === "string" && branch.baseRevision ? branch.baseRevision : null,
      sharedRevision: branch.sharedRevision,
      writerSessionId: branch.writerSessionId,
      createdAt: typeof branch.createdAt === "number" ? branch.createdAt : 0,
      updatedAt: typeof branch.updatedAt === "number" ? branch.updatedAt : 0,
      status: branch.status as DurableConflictStatus,
    };
  } catch {
    return null;
  }
}

function parseConflictIndex(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    const ids = Array.isArray(parsed)
      ? parsed
      : (parsed && typeof parsed === "object" ? (parsed as { conflictIds?: unknown }).conflictIds : null);
    return Array.isArray(ids)
      ? [...new Set(ids.filter((value): value is string => typeof value === "string" && Boolean(value)))]
      : [];
  } catch {
    return [];
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

/** 恢复副本的确定性 id：同一胶囊重处理指向同一分片，收敛而非重复。 */
export function recoveredCopyIdV3(conversationId: string, writerSessionId: string, sequence?: number): string {
  return `${conversationId}.recovered.${writerSessionId}${sequence === undefined ? "" : `.${sequence}`}`;
}

/** 单次启动对账回收孤儿胶囊的数量上限（本标签页胶囊不占预算）。 */
export const FOREIGN_RECOVERY_CAPSULE_BUDGET = 4;

/** 解析 `session.v3.recovery.<tabId>` 胶囊；任何字段非法都返回 null（按无有效条目处理）。 */
export function parseRecoveryCapsule(raw: string | null): RecoveryCapsule | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const capsule = parsed as Partial<RecoveryCapsule>;
    if (capsule.schemaVersion !== 2
      || typeof capsule.writerSessionId !== "string" || !capsule.writerSessionId
      || typeof capsule.sequence !== "number" || capsule.sequence < 1
      || typeof capsule.savedAt !== "number"
      || typeof capsule.digest !== "string" || !capsule.digest
      || !Array.isArray(capsule.entries)) return null;
    const entries: RecoveryCapsuleEntry[] = [];
    for (const value of capsule.entries) {
      if (!value || typeof value !== "object") return null;
      const entry = value as Partial<RecoveryCapsuleEntry>;
      if (typeof entry.conversationId !== "string" || !entry.conversationId
        || typeof entry.digest !== "string" || !entry.digest
        || !entry.conversation || typeof entry.conversation !== "object") return null;
      let payload: string;
      try {
        payload = JSON.stringify(entry.conversation);
      } catch {
        return null;
      }
      if (checkpointDigest(payload) !== entry.digest) return null;
      entries.push({
        conversationId: entry.conversationId,
        baseRevision: typeof entry.baseRevision === "string" && entry.baseRevision ? entry.baseRevision : null,
        digest: entry.digest,
        conversation: entry.conversation as Conversation,
        ...(entry.compaction ? { compaction: entry.compaction } : {}),
      });
    }
    const unsigned = JSON.stringify({
      schemaVersion: 2,
      writerSessionId: capsule.writerSessionId,
      sequence: capsule.sequence,
      savedAt: capsule.savedAt,
      entries,
    });
    if (checkpointDigest(unsigned) !== capsule.digest) return null;
    return {
      schemaVersion: 2,
      writerSessionId: capsule.writerSessionId,
      sequence: capsule.sequence,
      savedAt: capsule.savedAt,
      digest: capsule.digest,
      entries,
    };
  } catch {
    return null;
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
function loadV3Checkpoint(storage: StorageLike, conversationId: string, revision: string): ConversationCheckpointV3 | null {
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
  return {
    schemaVersion: 3,
    conversationId,
    revision,
    parentRevision: typeof checkpoint.parentRevision === "string" && checkpoint.parentRevision ? checkpoint.parentRevision : null,
    writerId: typeof checkpoint.writerId === "string" ? checkpoint.writerId : "",
    savedAt: typeof checkpoint.savedAt === "number" ? checkpoint.savedAt : 0,
    digest: checkpoint.digest,
    conversation: { ...conversation, messages: conversation.messages.map(checkpointMessage) },
    ...(checkpoint.compaction ? { compaction: checkpoint.compaction } : {}),
  };
}

function loadV3SnapshotConversation(storage: StorageLike, conversationId: string, revision: string): Conversation | null {
  return loadV3Checkpoint(storage, conversationId, revision)?.conversation ?? null;
}

/**
 * 枚举所有 V3 head，逐会话回读：head.revision 快照优先，损坏/缺失时回退
 * parentRevision。遇到 tombstone 的会话直接跳过（写入方在后续提交落地）。
 * 存储不支持枚举时返回 null，调用方继续走 V2 / legacy 读取链。
 */
function loadV3Conversations(storage: StorageLike): LoadedConversationShard[] | null {
  const headKeys = enumerateKeysWithPrefix(storage, conversationStorageKeys.v3HeadPrefix);
  if (!headKeys?.length) return null;
  const conversations: LoadedConversationShard[] = [];
  for (const headKey of headKeys) {
    const conversationId = headKey.slice(conversationStorageKeys.v3HeadPrefix.length);
    if (!conversationId) continue;
    if (safeGetItem(storage, sessionTombstoneKeyV3(conversationId)) !== null) continue;
    const head = parseV3Head(safeGetItem(storage, headKey));
    if (!head) continue;
    const advertised = loadV3SnapshotConversation(storage, conversationId, head.revision);
    const parent = !advertised && head.parentRevision
      ? loadV3SnapshotConversation(storage, conversationId, head.parentRevision)
      : null;
    const conversation = advertised ?? parent;
    if (conversation) {
      conversations.push({
        conversation,
        advertisedRevision: head.revision,
        loadedRevision: advertised ? head.revision : head.parentRevision as string,
        degraded: !advertised,
      });
    }
  }
  return conversations.length ? conversations : null;
}

function durableConflictBranches(storage: StorageLike, conversationId?: string): DurableConflictBranch[] {
  const branches: DurableConflictBranch[] = [];
  const conversationIds = conversationId
    ? [conversationId]
    : (enumerateKeysWithPrefix(storage, conversationStorageKeys.v3ConflictIndexPrefix) ?? [])
      .map((key) => key.slice(conversationStorageKeys.v3ConflictIndexPrefix.length))
      .filter(Boolean);
  for (const id of conversationIds) {
    const conflictIds = parseConflictIndex(safeGetItem(storage, sessionConflictIndexKeyV3(id)));
    for (const conflictId of conflictIds) {
      const branch = parseDurableConflictBranch(safeGetItem(storage, sessionConflictKeyV3(id, conflictId)));
      if (branch?.status === "pending") branches.push(branch);
    }
  }
  return branches.sort((left, right) => left.createdAt - right.createdAt || left.conflictId.localeCompare(right.conflictId));
}

function protectConflictChain(storage: StorageLike, keep: Set<string>, branch: DurableConflictBranch): void {
  let revision: string | null = branch.branchRevision;
  for (let depth = 0; revision && depth < 128; depth += 1) {
    if (keep.has(revision)) break;
    keep.add(revision);
    const checkpoint = loadV3Checkpoint(storage, branch.conversationId, revision);
    revision = checkpoint?.parentRevision ?? null;
    if (revision === branch.baseRevision) break;
  }
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

function collectStaleSnapshots(
  storage: StorageLike,
  conversationId: string,
  keep: ReadonlySet<string>,
  budget: number,
  currentWriterSessionId: string,
): void {
  const prefix = sessionSnapshotKeyV3(conversationId, "");
  const keys = enumerateKeysWithPrefix(storage, prefix);
  if (!keys) return; // 存储不支持枚举时静默跳过。
  const protectedRevisions = new Set(keep);
  protectLockFreeProposalSnapshots(storage, conversationId, protectedRevisions);
  const snapshotGcNow = Date.now();
  let remaining = budget;
  for (const key of keys) {
    if (remaining <= 0) return;
    const revision = key.slice(prefix.length);
    const revisionWriterSessionId = revision.slice(revision.indexOf(".") + 1);
    if (protectedRevisions.has(revision)
      || (revisionWriterSessionId !== currentWriterSessionId
        && isRecentVerifiedCheckpoint(storage, conversationId, revision, snapshotGcNow))) continue;
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
 * Proposal 只有在键、信封与快照 digest 三者一致时才保护快照；损坏或伪造的
 * Proposal 不得让孤儿快照永久逃过 GC。
 */
function protectLockFreeProposalSnapshots(storage: StorageLike, conversationId: string, keep: Set<string>): void {
  const keys = enumerateKeysWithPrefix(storage, `${conversationStorageKeys.v3ProposalPrefix}${conversationId}.`);
  if (!keys) return;
  for (const key of keys) {
    const proposal = parseLockFreeProposal(safeGetItem(storage, key));
    if (!proposal
      || proposal.conversationId !== conversationId
      || key !== sessionProposalKeyV3(conversationId, proposal.parentRevision, proposal.revision)) continue;
    const checkpoint = loadV3Checkpoint(storage, conversationId, proposal.revision);
    if (checkpoint?.digest === proposal.digest) keep.add(proposal.revision);
  }
}

function isRecentVerifiedCheckpoint(
  storage: StorageLike,
  conversationId: string,
  revision: string,
  now: number,
): boolean {
  const checkpoint = loadV3Checkpoint(storage, conversationId, revision);
  if (!checkpoint) return false;
  const age = now - checkpoint.savedAt;
  return age >= -CONVERSATION_CHECKPOINT_ORPHAN_GRACE_MS && age <= CONVERSATION_CHECKPOINT_ORPHAN_GRACE_MS;
}

/**
 * 空闲孤儿 GC：扫描所有 V3 快照，删除不被所属会话 head / parentRevision /
 * 冲突指针 / 可验证 Proposal 引用且已超过发布宽限期的快照（崩溃/失败写入
 * 的残留），单次最多 budget 份，返回删除数。
 * 同一份预算内继续回收 tombstone：仅当每个未过期标签页租约的 lastSeen 都晚于
 * tombstone.deletedAt（所有活跃标签页 provably 见过删除），且已超出保留窗口
 * 或数量上限（最旧先回收）时才删除；存储不支持枚举时全部保守保留。
 */
export function runIdleCheckpointGc(storage: StorageLike, budget = 4): number {
  const snapshotKeys = enumerateKeysWithPrefix(storage, conversationStorageKeys.v3SnapshotPrefix);
  if (!snapshotKeys) return 0;
  const snapshotGcNow = Date.now();
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
      for (const branch of durableConflictBranches(storage, conversationId)) {
        protectConflictChain(storage, keep, branch);
      }
      protectLockFreeProposalSnapshots(storage, conversationId, keep);
      keepByConversation.set(conversationId, keep);
    }
    return keep;
  };
  let removed = 0;
  for (const key of snapshotKeys) {
    if (removed >= budget) break;
    const parsed = parseSnapshotKey(key);
    if (!parsed) continue;
    if (keepSetFor(parsed.conversationId).has(parsed.revision)
      || isRecentVerifiedCheckpoint(storage, parsed.conversationId, parsed.revision, snapshotGcNow)) continue;
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
  /** 仅供确定性测试；生产默认始终为每个适配器文档实例生成 UUID。 */
  identity?: ReplicaIdentityOverrides;
  /** 仅供遗留的模块级测试适配器；Controller 永不启用。 */
  legacyWriterFromContinuity?: boolean;
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
  const localConflictBranches = new Map<string, LocalConflictBranchState>();
  const degradedHeads = new Map<string, { advertisedRevision: string; loadedRevision: string }>();
  const lastProposalKey = new Map<string, string>();
  const lastSettledProposalFingerprint = new Map<string, string>();
  /** Controller 已请求但尚未耐久提交的删除（pagehide 同步 flush 会先提交它们）。 */
  const pendingTombstones = new Set<string>();
  /** 本标签页已知被删除的会话：提交一律跳过（删除方自身状态 / 已物化过恢复副本）。 */
  const tombstonedRefused = new Set<string>();
  let lastHeadRevision: string | null = null;
  let idleGcScheduled = false;
  let identity: ReplicaIdentity | null = null;
  let capsuleSequence = 0;

  function getReplicaIdentity(session: StorageLike | null = browserSessionStorage()): ReplicaIdentity {
    identity ??= createReplicaIdentity(session, adapterOptions.legacyWriterFromContinuity
      ? { ...adapterOptions.identity, writerSessionId: adapterOptions.identity?.writerSessionId ?? getTabId(session) }
      : adapterOptions.identity);
    return identity;
  }

  function rotateWriterIdentity(session: StorageLike | null = browserSessionStorage()): ReplicaIdentity {
    const previous = getReplicaIdentity(session);
    identity = createReplicaIdentity(session, { documentInstanceId: previous.documentInstanceId });
    return identity;
  }

  function load(storage: StorageLike | null = browserStorage(), session: StorageLike | null = browserSessionStorage()): PersistedConversationState {
    if (!storage) return { schemaVersion: 1, currentConversationId: null, conversations: [] };
    // V3 分片优先；其后依次为 V2 journal（迁移读取器，保持原逻辑）与 legacy 键。
    const sharded = loadV3Conversations(storage);
    if (sharded) {
      const restoredById = new Map(sharded.map((entry) => [entry.conversation.id, entry.conversation]));
      const pending = durableConflictBranches(storage);
      const ownWriterSessionId = getReplicaIdentity(session).writerSessionId;
      for (const branch of pending) {
        if (branch.writerSessionId !== ownWriterSessionId) continue;
        const branchConversation = loadV3SnapshotConversation(storage, branch.conversationId, branch.branchRevision);
        if (!branchConversation || localConflictBranches.has(branch.conversationId)) continue;
        restoredById.set(branch.conversationId, branchConversation);
        localConflictBranches.set(branch.conversationId, {
          conversationId: branch.conversationId,
          conflictId: branch.conflictId,
          branchRevision: branch.branchRevision,
          baseRevision: branch.baseRevision,
          sharedRevision: branch.sharedRevision,
          status: "editing-branch",
        });
      }
      const conversations = sortConversations([...restoredById.values()]);
      const shardById = new Map(sharded.map((entry) => [entry.conversation.id, entry]));
      // V3 加载的分片内容与共享 head 一致：按"已提交"登记（identity + base），
      // 第二个打开的标签页因此不会重写共享 head，更不会在仲裁下制造伪冲突。
      // 只对进入 state 的会话登记（sortConversations 有数量上限），避免把被
      // 截断的会话误判为"本地已删除"。V2 / legacy 加载不登记——那些内容尚未
      // 落入 V3，必须按脏提交完成迁移。
      for (const conversation of conversations) {
        const shard = shardById.get(conversation.id);
        const localBranch = localConflictBranches.get(conversation.id);
        const headRevision = localBranch?.branchRevision ?? shard?.loadedRevision;
        if (!headRevision) continue;
        lastCommitted.set(conversation.id, conversation);
        lastCommittedRevision.set(conversation.id, headRevision);
        if (shard?.degraded) {
          degradedHeads.set(conversation.id, {
            advertisedRevision: shard.advertisedRevision,
            loadedRevision: shard.loadedRevision,
          });
        }
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
    | { ok: true; kind: "head"; revision: string; savedAt: number; compaction?: CheckpointCompaction }
    | {
        ok: true;
        kind: "conflict";
        revision: string;
        savedAt: number;
        baseRevision: string | null;
        sharedRevision: string;
        conflictId: string;
        compaction?: CheckpointCompaction;
      }
    | PersistenceFlushFailure;

  /** 单次提交尝试的结果：失败额外携带 quota 标记（配额失败才触发压缩重试）。 */
  type ShardCommitAttempt =
    | { ok: true; kind: "head"; revision: string; savedAt: number }
    | {
        ok: true;
        kind: "conflict";
        revision: string;
        savedAt: number;
        baseRevision: string | null;
        sharedRevision: string;
        conflictId: string;
      }
    | { ok: false; quota: boolean; failure: PersistenceFlushFailure };

  function conflictIdFor(conversationId: string, writerSessionId: string, firstRevision: string): string {
    return checkpointDigest(`${conversationId}\u0000${writerSessionId}\u0000${firstRevision}`);
  }

  function writeConflictLedger(
    storage: StorageLike,
    conversationId: string,
    revision: string,
    parentBranchRevision: string | null,
    baseRevision: string | null,
    sharedRevision: string,
    writerSessionId: string,
    savedAt: number,
    preferredConflictId?: string,
  ): DurableConflictBranch | PersistenceFlushFailure {
    const existing = preferredConflictId
      ? parseDurableConflictBranch(safeGetItem(storage, sessionConflictKeyV3(conversationId, preferredConflictId)))
      : null;
    const conflictId = preferredConflictId ?? conflictIdFor(conversationId, writerSessionId, revision);
    const branch: DurableConflictBranch = {
      schemaVersion: 1,
      conflictId,
      conversationId,
      branchRevision: revision,
      parentBranchRevision,
      baseRevision,
      sharedRevision,
      writerSessionId,
      createdAt: existing?.createdAt ?? savedAt,
      updatedAt: savedAt,
      status: "pending",
    };
    const serialized = JSON.stringify(branch);
    const pointer: ConversationConflictPointer = {
      revision,
      baseRevision,
      sharedRevision,
      writerId: writerSessionId,
      savedAt,
    };
    try {
      storage.setItem(sessionConflictKeyV3(conversationId, conflictId), serialized);
      if (storage.getItem(sessionConflictKeyV3(conversationId, conflictId)) !== serialized) {
        return conflictFailure("账本写入后回读核验失败", conversationId, 0);
      }
      // 兼容 4.3.6 读取器，同时为 index 写入失败的极窄窗口保留快照保护。
      storage.setItem(sessionConflictKeyV3(conversationId), JSON.stringify(pointer));
      const ids = parseConflictIndex(safeGetItem(storage, sessionConflictIndexKeyV3(conversationId)));
      if (!ids.includes(conflictId)) ids.push(conflictId);
      const indexSerialized = JSON.stringify({ schemaVersion: 1, conflictIds: ids });
      storage.setItem(sessionConflictIndexKeyV3(conversationId), indexSerialized);
      if (storage.getItem(sessionConflictIndexKeyV3(conversationId)) !== indexSerialized) {
        return conflictFailure("索引写入后回读核验失败", conversationId, 0);
      }
    } catch (error) {
      const failure = classifyStorageError(error);
      return conflictFailure(`账本写入失败：${failure.message}`, conversationId, 0);
    }
    return branch;
  }

  function siblingProposals(storage: StorageLike, conversationId: string, parentRevision: string | null): LockFreeProposal[] {
    const keys = enumerateKeysWithPrefix(
      storage,
      `${conversationStorageKeys.v3ProposalPrefix}${conversationId}.${parentRevision ?? "root"}.`,
    ) ?? [];
    const proposals: LockFreeProposal[] = [];
    for (const key of keys) {
      const proposal = parseLockFreeProposal(safeGetItem(storage, key));
      if (!proposal || proposal.conversationId !== conversationId || proposal.parentRevision !== parentRevision) continue;
      const checkpoint = loadV3Checkpoint(storage, conversationId, proposal.revision);
      if (!checkpoint || checkpoint.digest !== proposal.digest) continue;
      proposals.push(proposal);
    }
    return proposals.sort((left, right) =>
      left.logicalSequence - right.logicalSequence
      || left.savedAt - right.savedAt
      || left.writerSessionId.localeCompare(right.writerSessionId));
  }

  function proposalFingerprint(proposals: LockFreeProposal[]): string {
    return proposals.map((proposal) =>
      `${proposal.revision}\u0000${proposal.writerSessionId}\u0000${proposal.digest}`).join("\u0001");
  }

  /**
   * 对同一 parent 的可见 Proposal 做幂等最终仲裁。首次提交后还会在下一个
   * macrotask 再执行一次：跨 renderer 的 localStorage 可见性可能晚于同步
   * setItem 返回，只有再次收集才能把各自先看到自己的真正并发提交收敛为
   * 同一个 Head，并为所有负方补齐 Conflict Ledger。
   */
  function settleLockFreeProposal(
    storage: StorageLike,
    ownProposal: LockFreeProposal,
    bytes = 0,
  ): ShardCommitAttempt {
    const {
      conversationId,
      parentRevision,
      revision,
      savedAt,
      writerSessionId,
    } = ownProposal;
    const proposals = siblingProposals(storage, conversationId, parentRevision);
    const winner = proposals[0];
    if (!winner) {
      return { ok: false, quota: false, failure: verificationFailure("没有可验证的 Proposal") };
    }
    const currentHead = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)));
    const currentIsParent = currentHead?.revision === parentRevision;
    const currentIsSibling = currentHead?.parentRevision === parentRevision
      && revisionSeq(currentHead.revision) === revisionSeq(winner.revision);
    if ((currentHead === null && parentRevision === null) || currentIsParent || currentIsSibling) {
      const winnerCheckpoint = loadV3Checkpoint(storage, conversationId, winner.revision);
      if (!winnerCheckpoint) {
        return { ok: false, quota: false, failure: verificationFailure("Winner 快照核验失败") };
      }
      try {
        storage.setItem(sessionHeadKeyV3(conversationId), JSON.stringify({
          revision: winner.revision,
          parentRevision,
          writerId: winner.writerSessionId,
          savedAt: winner.savedAt,
          digest: winner.digest,
        } satisfies ConversationHeadV3));
      } catch (error) {
        const failure = classifyStorageError(error);
        return {
          ok: false,
          quota: failure.code === "quota-exceeded",
          failure: shardFailure(failure, conversationId, bytes),
        };
      }
      for (const loser of proposals.slice(1)) {
        const ledger = writeConflictLedger(
          storage,
          conversationId,
          loser.revision,
          null,
          parentRevision,
          winner.revision,
          loser.writerSessionId,
          loser.savedAt,
        );
        if ("ok" in ledger) return { ok: false, quota: false, failure: ledger };
        if (loser.writerSessionId === writerSessionId) {
          localConflictBranches.set(conversationId, {
            conversationId,
            conflictId: ledger.conflictId,
            branchRevision: loser.revision,
            baseRevision: parentRevision,
            sharedRevision: winner.revision,
            status: "editing-branch",
          });
        }
      }
      writeTabLease(storage, writerSessionId);
      lastSettledProposalFingerprint.set(conversationId, proposalFingerprint(proposals));
      if (winner.revision === revision) {
        return { ok: true, kind: "head", revision, savedAt };
      }
      const own = durableConflictBranches(storage, conversationId)
        .find((branch) => branch.branchRevision === revision && branch.writerSessionId === writerSessionId);
      return {
        ok: true,
        kind: "conflict",
        revision,
        savedAt,
        baseRevision: parentRevision,
        sharedRevision: winner.revision,
        conflictId: own?.conflictId ?? conflictIdFor(conversationId, writerSessionId, revision),
      };
    }
    // Head 已经离开同胞层级：旧 Proposal 只能成为隔离分支，绝不回退或覆盖 Head。
    if (currentHead) {
      const ledger = writeConflictLedger(
        storage,
        conversationId,
        revision,
        null,
        parentRevision,
        currentHead.revision,
        writerSessionId,
        savedAt,
      );
      if ("ok" in ledger) return { ok: false, quota: false, failure: ledger };
      localConflictBranches.set(conversationId, {
        conversationId,
        conflictId: ledger.conflictId,
        branchRevision: revision,
        baseRevision: parentRevision,
        sharedRevision: currentHead.revision,
        status: "editing-branch",
      });
      writeTabLease(storage, writerSessionId);
      lastSettledProposalFingerprint.set(conversationId, proposalFingerprint(proposals));
      return {
        ok: true,
        kind: "conflict",
        revision,
        savedAt,
        baseRevision: parentRevision,
        sharedRevision: currentHead.revision,
        conflictId: ledger.conflictId,
      };
    }
    return { ok: false, quota: false, failure: verificationFailure("Proposal 仲裁期间共享 Head 消失") };
  }

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
   * 配额耗尽（quota-exceeded）时不直接失败，而是以同一 revision 渐进重试：
   * 原始写入 → level 1（剥离可重建的大图预览，附件元信息与全部文本保留）→
   * level 2（再为超大 timeline 原始 payload 设上限）→ 仍失败则以
   * storage-pressure 收束：旧 head / 旧快照原样保留，绝不静默删数据换空间。
   */
  function commitConversationShard(
    storage: StorageLike,
    conversation: Conversation,
    writerSessionId: string,
    mode: "exclusive" | "proposal" = "exclusive",
  ): ShardCommitOutcome {
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
    if (!head && localBase && !localConflictBranches.has(conversation.id)) {
      return conflictFailure("共享 Head 已缺失，拒绝复活原会话 ID", conversation.id, bytes);
    }
    const activeBranch = localConflictBranches.get(conversation.id);
    const conflicted = Boolean(activeBranch) || (head !== null && sharedRevision !== localBase);
    const revision = `${Math.max(revisionSeq(sharedRevision), revisionSeq(activeBranch?.branchRevision ?? null)) + 1}.${writerSessionId}`;
    const parentRevision = activeBranch?.branchRevision ?? (conflicted ? localBase : sharedRevision);
    // 失败形状与 4.3.6 前完全一致；quota 标记单独携带，供外层决定是否升级压缩级别。
    const fail = (error: unknown): { quota: boolean; failure: PersistenceFlushFailure } => {
      const classified = classifyStorageError(error);
      return {
        quota: classified.code === "quota-exceeded",
        failure: conflicted
          ? conflictFailure(`写入失败：${classified.message}`, conversation.id, bytes)
          : shardFailure(classified, conversation.id, bytes),
      };
    };

    /**
     * 单次提交尝试（snapshot 写入 → 回读核验 → head / 冲突指针推进）。
     * candidate 是本次落盘的会话形态：level 0 为原始会话，更高 level 为
     * compactConversationForStorage 的确定性压缩结果；compaction 非空时写入
     * checkpoint 信封（digest 只覆盖会话负载，不受信封字段影响）。revision
     * 跨尝试不变，重试覆写同一快照键。
     */
    const attempt = (candidate: Conversation, compaction?: CheckpointCompaction): ShardCommitAttempt => {
      let serialized: string;
      let digest: string;
      let savedAt = 0;
      try {
        const normalized = normalizeConversationForCommit(candidate);
        const payload = JSON.stringify(normalized);
        digest = checkpointDigest(payload);
        savedAt = Date.now();
        const checkpoint: ConversationCheckpointV3 = {
          schemaVersion: 3,
          conversationId: conversation.id,
          revision,
          parentRevision,
          writerId: writerSessionId,
          savedAt,
          digest,
          conversation: normalized,
        };
        if (compaction) checkpoint.compaction = compaction;
        serialized = JSON.stringify(checkpoint);
      } catch (error) {
        return { ok: false, ...fail(error) };
      }
      const snapshotKey = sessionSnapshotKeyV3(conversation.id, revision);
      try {
        const existingRaw = storage.getItem(snapshotKey);
        if (existingRaw !== null) {
          const existing = loadV3Checkpoint(storage, conversation.id, revision);
          if (!existing || existing.digest !== digest) {
            return {
              ok: false,
              quota: false,
              failure: conflictFailure("不可变快照 revision 发生碰撞", conversation.id, bytes),
            };
          }
          serialized = existingRaw;
          savedAt = existing.savedAt;
          digest = existing.digest;
        } else {
          storage.setItem(snapshotKey, serialized);
          // 回读核验：字节级比对刚写入的快照，任何损坏都视为整份不可用。
          if (storage.getItem(snapshotKey) !== serialized) {
            return {
              ok: false,
              quota: false,
              failure: conflicted
                ? conflictFailure("写入后回读核验失败", conversation.id, bytes)
                : shardFailure(verificationFailure("快照写入后回读核验失败"), conversation.id, bytes),
            };
          }
        }
      } catch (error) {
        return { ok: false, ...fail(error) };
      }
      if (activeBranch || (mode === "exclusive" && conflicted && head)) {
        const ledger = writeConflictLedger(
          storage,
          conversation.id,
          revision,
          activeBranch?.branchRevision ?? null,
          activeBranch?.baseRevision ?? localBase,
          head?.revision ?? activeBranch?.sharedRevision ?? "",
          writerSessionId,
          savedAt,
          activeBranch?.conflictId,
        );
        if ("ok" in ledger) return { ok: false, quota: false, failure: ledger };
        localConflictBranches.set(conversation.id, {
          conversationId: conversation.id,
          conflictId: ledger.conflictId,
          branchRevision: revision,
          baseRevision: ledger.baseRevision,
          sharedRevision: ledger.sharedRevision,
          status: "editing-branch",
        });
        writeTabLease(storage, writerSessionId);
        return {
          ok: true,
          kind: "conflict",
          revision,
          savedAt,
          baseRevision: ledger.baseRevision,
          sharedRevision: ledger.sharedRevision,
          conflictId: ledger.conflictId,
        };
      }
      if (mode === "proposal") {
        const proposal: LockFreeProposal = {
          schemaVersion: 1,
          conversationId: conversation.id,
          parentRevision: localBase,
          revision,
          logicalSequence: revisionSeq(revision),
          savedAt,
          writerSessionId,
          digest,
        };
        const proposalKey = sessionProposalKeyV3(conversation.id, localBase, revision);
        const proposalSerialized = JSON.stringify(proposal);
        const retirePreviousProposal = (): void => {
          const previousKey = lastProposalKey.get(conversation.id);
          lastProposalKey.set(conversation.id, proposalKey);
          if (!previousKey || previousKey === proposalKey) return;
          try {
            storage.removeItem(previousKey);
          } catch {
            // 已有 Head / Ledger 是真相来源；残留 Proposal 只增加后续扫描成本。
          }
        };
        try {
          const existing = storage.getItem(proposalKey);
          if (existing !== null && existing !== proposalSerialized) {
            return { ok: false, quota: false, failure: conflictFailure("Proposal 键发生碰撞", conversation.id, bytes) };
          }
          storage.setItem(proposalKey, proposalSerialized);
          if (storage.getItem(proposalKey) !== proposalSerialized) {
            return { ok: false, quota: false, failure: verificationFailure("Proposal 写入后回读核验失败") };
          }
        } catch (error) {
          return { ok: false, ...fail(error) };
        }
        const settlement = settleLockFreeProposal(storage, proposal, bytes);
        if (settlement.ok) {
          retirePreviousProposal();
        } else {
          // 同步提交失败不会安排后续仲裁；撤销 Proposal，使已写快照在发布
          // 宽限期后恢复为可回收孤儿，而不是被一个永不重试的意图永久保护。
          try {
            storage.removeItem(proposalKey);
          } catch {
            // 残留 Proposal 最多延迟 GC；失败结果已通过健康通道上报。
          }
        }
        return settlement;
      }
      try {
        storage.setItem(sessionHeadKeyV3(conversation.id), JSON.stringify({
          revision,
          parentRevision,
          writerId: writerSessionId,
          savedAt,
          digest,
        } satisfies ConversationHeadV3));
      } catch (error) {
        // head 推进只在非冲突路径到达，失败形状与 4.3.6 前一致（shardFailure）。
        return { ok: false, ...fail(error) };
      }
      writeTabLease(storage, writerSessionId);
      // 保留式 GC：有界 O(1)——最多删除 2 份既非当前、也非 parentRevision、
      // 也非冲突指针引用分支的旧快照。
      const keep = new Set(parentRevision ? [revision, parentRevision] : [revision]);
      const conflict = parseV3ConflictPointer(safeGetItem(storage, sessionConflictKeyV3(conversation.id)));
      if (conflict) keep.add(conflict.revision);
      for (const branch of durableConflictBranches(storage, conversation.id)) {
        protectConflictChain(storage, keep, branch);
      }
      collectStaleSnapshots(storage, conversation.id, keep, 2, writerSessionId);
      return { ok: true, kind: "head", revision, savedAt };
    };

    // 配额降级的确定性重试链：只有 quota-exceeded 才进入下一级，其余失败原样返回。
    let lastQuotaFailure: PersistenceFlushFailure | null = null;
    for (const level of [0, 1, 2]) {
      const compacted = level === 0
        ? { conversation, removedPreviewBytes: 0 }
        : compactConversationForStorage(conversation, level);
      const compaction: CheckpointCompaction | undefined = level === 0
        ? undefined
        : { level, removedPreviewBytes: compacted.removedPreviewBytes, reason: "storage-pressure" };
      const outcome = attempt(compacted.conversation, compaction);
      if (outcome.ok) {
        if (!compaction) return outcome;
        return { ...outcome, compaction };
      }
      if (!outcome.quota) return outcome.failure;
      lastQuotaFailure = outcome.failure;
    }
    // 全部压缩级别仍超限：storage-pressure 收束（message 沿用最后一次配额失败的描述）。
    const finalFailure = lastQuotaFailure
      ?? shardFailure({ ok: false, code: "quota-exceeded", message: "存储配额耗尽" }, conversation.id, bytes);
    return { ...finalFailure, code: "storage-pressure" };
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
    const protectedRevisions = new Set(durableConflictBranches(storage, conversationId).flatMap((branch) =>
      branch.parentBranchRevision ? [branch.branchRevision, branch.parentBranchRevision] : [branch.branchRevision]));
    if (knownRevision && !protectedRevisions.has(knownRevision)) drop(sessionSnapshotKeyV3(conversationId, knownRevision));
    let removed = knownRevision ? 1 : 0;
    for (const key of enumerateKeysWithPrefix(storage, sessionSnapshotKeyV3(conversationId, "")) ?? []) {
      if (removed >= 2) break;
      if (key === sessionSnapshotKeyV3(conversationId, knownRevision ?? "")) continue;
      const revision = key.slice(sessionSnapshotKeyV3(conversationId, "").length);
      if (protectedRevisions.has(revision)) continue;
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

  /** 仅在排他仲裁内修复“Head 广告损坏、parent 有效”的降级链。 */
  function healDegradedHeads(storage: StorageLike, options?: SaveConversationOptions): void {
    for (const [conversationId, degraded] of degradedHeads) {
      const current = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)));
      if (!current || current.revision !== degraded.advertisedRevision) {
        degradedHeads.delete(conversationId);
        continue;
      }
      const loaded = loadV3Checkpoint(storage, conversationId, degraded.loadedRevision);
      if (!loaded) continue;
      const quarantineKey = `${conversationStorageKeys.v3QuarantinePrefix}head.${conversationId}.${degraded.advertisedRevision}`;
      try {
        const quarantine = JSON.stringify({
          schemaVersion: 1,
          kind: "corrupt-head-snapshot",
          conversationId,
          advertisedRevision: degraded.advertisedRevision,
          recoveredRevision: degraded.loadedRevision,
          quarantinedAt: Date.now(),
        });
        storage.setItem(quarantineKey, quarantine);
        storage.setItem(sessionHeadKeyV3(conversationId), JSON.stringify({
          revision: loaded.revision,
          parentRevision: loaded.parentRevision,
          writerId: loaded.writerId,
          savedAt: loaded.savedAt,
          digest: loaded.digest,
        } satisfies ConversationHeadV3));
        degradedHeads.delete(conversationId);
        options?.onWarning?.(verificationFailure(`会话 ${conversationId} 的损坏 Head 已回退并修复`));
      } catch {
        // 保留 degraded 标记和有效 parent 的 GC 保护，下次排他提交继续尝试。
      }
    }
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
   * 配额耗尽先按 level 1 / level 2 渐进压缩重试同一提交（成功经 onCompaction
   * 带外上报）；全部级别仍超限返回 storage-pressure，旧 head 原样保留。
   * 任何路径都不抛异常，失败以 PersistenceFlushResult 返回并附估算负载大小。
   * 提交落地后顺手做胶囊保鲜：脏集合被正常提交耗尽 ⇒ 移除本标签页应急胶囊；
   *   只有部分会话提交成功 ⇒ 以剩余脏集合重写既有胶囊（无胶囊则不新建——
   *   进行中的保存失败走健康/横幅链路，胶囊只在页面退出时写入）。
   */
  function save(
    state: PersistedConversationState,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
    options?: SaveConversationOptions,
  ): PersistenceFlushResult {
    if (!storage) return storageUnavailableFailure(`localStorage 不可用 (~${JSON.stringify(state).length} bytes)`);
    const writerSessionId = getReplicaIdentity(session).writerSessionId;
    writeTabSelection(session, state.currentConversationId);
    // pagehide / beforeunload 无法同步取得 Web Lock：绝不伪装成已仲裁，也不碰共享 Head。
    if (options?.lifecycle) return { ok: true };
    const result = saveShards(
      state,
      storage,
      writerSessionId,
      options,
      adapterOptions.legacyWriterFromContinuity ? "exclusive" : "proposal",
    );
    refreshRecoveryCapsule(storage, writerSessionId, state);
    return result;
  }

  function saveShards(
    state: PersistedConversationState,
    storage: StorageLike,
    writerSessionId: string,
    options?: SaveConversationOptions,
    mode: "exclusive" | "proposal" = "exclusive",
  ): PersistenceFlushResult {

    // 待提交 tombstone 优先（Controller 请求的删除；pagehide 同步 flush 也在此提交）。
    for (const conversationId of pendingTombstones) {
      const failure = commitTombstone(storage, conversationId, writerSessionId, options);
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
      // Tombstone 可能早已按保留策略被 GC；只要本地见过 base 而共享 Head 缺失，
      // 原 ID 就不再具备创建权，只能物化恢复副本。
      if (lastCommittedRevision.has(conversation.id)
        && !localConflictBranches.has(conversation.id)
        && parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversation.id))) === null) {
        tombstonedRefused.add(conversation.id);
        lastCommitted.delete(conversation.id);
        lastCommittedRevision.delete(conversation.id);
        const copy = copyConversation(conversation, "（恢复副本）");
        queue.push(copy);
        options?.onRecovery?.({ conversationId: conversation.id, copy });
        continue;
      }
      const outcome = commitConversationShard(storage, conversation, writerSessionId, mode);
      if (!outcome.ok) return outcome;
      // 冲突分支登记自己的 branch revision；绝不冒充共享 Head 的干净副本。
      lastCommitted.set(conversation.id, conversation);
      lastCommittedRevision.set(conversation.id, outcome.revision);
      lastHeadRevision = outcome.revision;
      options?.onCommit?.({ conversationId: conversation.id, revision: outcome.revision, writerId: writerSessionId, savedAt: outcome.savedAt });
      // 存储压力下带压缩成功的提交：一次性带外上报（预览已剥离，全部文字保留）。
      if (outcome.compaction) {
        options?.onCompaction?.({
          conversationId: conversation.id,
          revision: outcome.revision,
          level: outcome.compaction.level,
          removedPreviewBytes: outcome.compaction.removedPreviewBytes,
        });
      }
      if (outcome.kind === "conflict") {
        options?.onConflict?.({
          conversationId: conversation.id,
          conflictId: outcome.conflictId,
          title: conversation.title,
          revision: outcome.revision,
          baseRevision: outcome.baseRevision,
          sharedRevision: outcome.sharedRevision,
          writerId: writerSessionId,
          savedAt: outcome.savedAt,
        });
      }
    }

    // 此刻 state 中所有会话都已核验落入 V3，才允许清理 V2 / legacy 键。
    cleanupMigratedKeys(storage);
    scheduleIdleCheckpointGc(storage);
    return lastHeadRevision ? { ok: true, revision: lastHeadRevision } : { ok: true };
  }

  /** 锁系统失败才走 Proposal；回调已开始后抛错必须原样传播，绝不执行第二遍。 */
  async function arbitrate<T>(exclusive: () => T | Promise<T>, proposalFallback: () => T | Promise<T>): Promise<T> {
    const locks = adapterOptions.locks === undefined ? detectNavigatorLocks() : adapterOptions.locks;
    if (!locks) return proposalFallback();
    let callbackStarted = false;
    try {
      return await locks.request(CONVERSATION_CHECKPOINT_LOCK_NAME, { mode: "exclusive" }, () => {
        callbackStarted = true;
        return exclusive();
      });
    } catch (error) {
      if (callbackStarted) throw error;
      return proposalFallback();
    }
  }

  function settlePendingLockFreeProposals(
    storage: StorageLike,
    writerSessionId: string,
    options?: SaveConversationOptions,
  ): PersistenceFlushResult {
    for (const [conversationId, proposalKey] of lastProposalKey) {
      const proposal = parseLockFreeProposal(safeGetItem(storage, proposalKey));
      if (!proposal || proposal.writerSessionId !== writerSessionId) continue;
      const visible = siblingProposals(storage, conversationId, proposal.parentRevision);
      if (proposalFingerprint(visible) === lastSettledProposalFingerprint.get(conversationId)) continue;
      const previousConflictId = localConflictBranches.get(conversationId)?.conflictId;
      const committed = lastCommitted.get(conversationId);
      const settlement = settleLockFreeProposal(
        storage,
        proposal,
        committed ? estimateConversationBytes(committed) : 0,
      );
      if (!settlement.ok) return settlement.failure;
      if (settlement.kind !== "conflict" || settlement.conflictId === previousConflictId) continue;
      options?.onConflict?.({
        conversationId,
        conflictId: settlement.conflictId,
        title: committed?.title ?? "",
        revision: settlement.revision,
        baseRevision: settlement.baseRevision,
        sharedRevision: settlement.sharedRevision,
        writerId: writerSessionId,
        savedAt: settlement.savedAt,
      });
    }
    return { ok: true };
  }

  function schedulePendingLockFreeSettlement(
    storage: StorageLike,
    writerSessionId: string,
    options?: SaveConversationOptions,
  ): void {
    // 跨 renderer 的 localStorage 发布可能晚于同步 setItem 返回。提交 Promise 不被
    // 这段最终仲裁阻塞（避免拖住 autosave single-flight）；0ms 与 25ms 两次有界
    // 重扫只在 Proposal 集合真的增长时写入，失败通过既有健康通道上报。
    const settle = (): void => {
      const result = settlePendingLockFreeProposals(storage, writerSessionId, options);
      if (!result.ok) options?.onWarning?.(result);
    };
    globalThis.setTimeout(settle, 0);
    globalThis.setTimeout(settle, 25);
  }

  function saveArbitrated(
    getState: () => PersistedConversationState,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
    options?: SaveConversationOptions,
  ): Promise<PersistenceFlushResult> {
    if (!storage) return Promise.resolve(storageUnavailableFailure("localStorage 不可用"));
    const writerSessionId = getReplicaIdentity(session).writerSessionId;
    const run = (mode: "exclusive" | "proposal"): PersistenceFlushResult => {
      const state = getState();
      writeTabSelection(session, state.currentConversationId);
      const result = saveShards(state, storage, writerSessionId, options, mode);
      refreshRecoveryCapsule(storage, writerSessionId, state);
      return result;
    };
    return arbitrate(
      () => {
        healDegradedHeads(storage, options);
        return run("exclusive");
      },
      () => {
        const result = run("proposal");
        if (result.ok) schedulePendingLockFreeSettlement(storage, writerSessionId, options);
        return result;
      },
    );
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
    const writerSessionId = getReplicaIdentity(session).writerSessionId;
    pendingTombstones.add(conversationId);
    const commit = () => {
      const failure = commitTombstone(storage, conversationId, writerSessionId, options);
      pendingTombstones.delete(conversationId);
      return failure ?? { ok: true };
    };
    return arbitrate(commit, commit);
  }

  function setTabLease(
    active: boolean,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
  ): void {
    if (active && storage) {
      writeTabLease(storage, getReplicaIdentity(session).writerSessionId);
      return;
    }
    try {
      storage?.removeItem(`${conversationStorageKeys.v3TabPrefix}${getReplicaIdentity(session).writerSessionId}`);
    } catch {
      // best-effort：未删除的租约过期后自然不再阻挡 GC。
    }
  }

  function persistSelection(conversationId: string | null, session: StorageLike | null = browserSessionStorage()): void {
    writeTabSelection(session, conversationId);
  }

  function reconcileRemoteCommit(
    conversationId: string,
    local: Conversation | undefined,
    storage: StorageLike | null = browserStorage(),
  ): ReconcileRemoteOutcome {
    if (!storage) return { kind: "noop" };
    const head = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)));
    if (head && lastCommittedRevision.get(conversationId) === head.revision) return { kind: "noop" };
    // 已隔离的本地冲突分支即使刚刚完成一次耐久提交，也只相对 branch revision
    // 是“干净”的；它绝不能因胜方后续广播而换入共享 Head、丢失分支内容。
    if (localConflictBranches.has(conversationId)) return { kind: "stale" };
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
    const loadedRevision = loadV3SnapshotConversation(storage, conversationId, head.revision)
      ? head.revision
      : head.parentRevision as string;
    lastCommitted.set(conversationId, conversation);
    lastCommittedRevision.set(conversationId, loadedRevision);
    return { kind: "reload", conversation };
  }

  function readSharedConversation(
    conversationId: string,
    storage: StorageLike | null = browserStorage(),
  ): { conversation: Conversation; revision: string } | null {
    if (!storage) return null;
    const head = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)));
    if (!head) return null;
    const advertised = loadV3SnapshotConversation(storage, conversationId, head.revision);
    const parent = !advertised && head.parentRevision
      ? loadV3SnapshotConversation(storage, conversationId, head.parentRevision)
      : null;
    const conversation = advertised ?? parent;
    return conversation ? { conversation, revision: advertised ? head.revision : head.parentRevision as string } : null;
  }

  function readConflictBranch(
    conversationId: string,
    storage: StorageLike | null = browserStorage(),
    conflictId?: string,
  ): { pointer: ConversationConflictPointer; branch: DurableConflictBranch; conversation: Conversation } | null {
    if (!storage) return null;
    const branch = durableConflictBranches(storage, conversationId)
      .find((candidate) => !conflictId || candidate.conflictId === conflictId);
    if (!branch) return null;
    const conversation = loadV3SnapshotConversation(storage, conversationId, branch.branchRevision);
    if (!conversation) return null;
    return {
      branch,
      pointer: {
        revision: branch.branchRevision,
        baseRevision: branch.baseRevision,
        sharedRevision: branch.sharedRevision,
        writerId: branch.writerSessionId,
        savedAt: branch.updatedAt,
      },
      conversation,
    };
  }

  function listConflictBranches(conversationId?: string, storage: StorageLike | null = browserStorage()): DurableConflictBranch[] {
    return storage ? durableConflictBranches(storage, conversationId) : [];
  }

  function removeConflictRecord(storage: StorageLike, branch: DurableConflictBranch): void {
    const ids = parseConflictIndex(safeGetItem(storage, sessionConflictIndexKeyV3(branch.conversationId)))
      .filter((id) => id !== branch.conflictId);
    try {
      if (ids.length) {
        storage.setItem(sessionConflictIndexKeyV3(branch.conversationId), JSON.stringify({ schemaVersion: 1, conflictIds: ids }));
      } else {
        storage.removeItem(sessionConflictIndexKeyV3(branch.conversationId));
      }
      storage.removeItem(sessionConflictKeyV3(branch.conversationId, branch.conflictId));
      const next = durableConflictBranches(storage, branch.conversationId)[0];
      if (next) {
        storage.setItem(sessionConflictKeyV3(branch.conversationId), JSON.stringify({
          revision: next.branchRevision,
          baseRevision: next.baseRevision,
          sharedRevision: next.sharedRevision,
          writerId: next.writerSessionId,
          savedAt: next.updatedAt,
        } satisfies ConversationConflictPointer));
      } else {
        storage.removeItem(sessionConflictKeyV3(branch.conversationId));
      }
      // 分支已解决后 Proposal 不再是待仲裁意图；否则它会在 Ledger 释放后
      // 继续保护同一快照，使已解决分支无法由空闲 GC 回收。
      storage.removeItem(sessionProposalKeyV3(branch.conversationId, branch.baseRevision, branch.branchRevision));
      if (localConflictBranches.get(branch.conversationId)?.conflictId === branch.conflictId) {
        localConflictBranches.delete(branch.conversationId);
      }
    } catch {
      // resolved 状态已经耐久；残留索引/记录只会使下一次启动继续完成清理。
    }
  }

  function finalizeConflictRecord(storage: StorageLike, branch: DurableConflictBranch, status: "resolved-copy" | "discarded"): boolean {
    const resolved: DurableConflictBranch = { ...branch, status, updatedAt: Date.now() };
    const serialized = JSON.stringify(resolved);
    try {
      storage.setItem(sessionConflictKeyV3(branch.conversationId, branch.conflictId), serialized);
      if (storage.getItem(sessionConflictKeyV3(branch.conversationId, branch.conflictId)) !== serialized) return false;
    } catch {
      return false;
    }
    removeConflictRecord(storage, resolved);
    return true;
  }

  function clearConflict(
    conversationId: string,
    storage: StorageLike | null = browserStorage(),
    conflictId?: string,
  ): void {
    if (!storage) return;
    const branch = durableConflictBranches(storage, conversationId)
      .find((candidate) => !conflictId || candidate.conflictId === conflictId);
    if (branch) removeConflictRecord(storage, branch);
  }

  function resolveConflictByCopyArbitrated(
    conversationId: string,
    conflictId: string,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
    options?: SaveConversationOptions,
  ): Promise<{ ok: true; copy: Conversation } | PersistenceFlushFailure> {
    if (!storage) return Promise.resolve(storageUnavailableFailure("localStorage 不可用"));
    const writerSessionId = getReplicaIdentity(session).writerSessionId;
    const transaction = (mode: "exclusive" | "proposal"): { ok: true; copy: Conversation } | PersistenceFlushFailure => {
      const record = parseDurableConflictBranch(safeGetItem(storage, sessionConflictKeyV3(conversationId, conflictId)));
      const copyId = `${conversationId}.conflict.${conflictId}`;
      if (!record) {
        const existing = readSharedConversation(copyId, storage);
        return existing ? { ok: true, copy: existing.conversation } : verificationFailure("冲突记录不存在或已损坏");
      }
      const branchConversation = loadV3SnapshotConversation(storage, conversationId, record.branchRevision);
      if (!branchConversation) return verificationFailure("冲突分支快照核验失败");
      const copy: Conversation = {
        ...branchConversation,
        id: copyId,
        title: `${branchConversation.title}（冲突副本）`,
        customTitle: true,
        favorite: false,
        createdAt: record.createdAt,
        updatedAt: record.updatedAt,
      };
      let expectedDigest: string;
      try {
        expectedDigest = checkpointDigest(JSON.stringify(normalizeConversationForCommit(copy)));
      } catch (error) {
        return classifyStorageError(error);
      }
      const existingHead = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(copyId)));
      if (existingHead && existingHead.digest !== expectedDigest) {
        return conflictFailure("稳定副本 ID 已被不同内容占用", conversationId, estimateConversationBytes(copy));
      }
      if (!existingHead) {
        lastCommittedRevision.delete(copyId);
        const committed = commitConversationShard(storage, copy, writerSessionId, mode);
        if (!committed.ok) return committed;
        if (committed.kind !== "head") return conflictFailure("独立副本未取得自己的 Head", conversationId, estimateConversationBytes(copy));
        options?.onCommit?.({
          conversationId: copyId,
          revision: committed.revision,
          writerId: writerSessionId,
          savedAt: committed.savedAt,
        });
      }
      const verifiedHead = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(copyId)));
      const verified = verifiedHead?.digest === expectedDigest
        ? loadV3SnapshotConversation(storage, copyId, verifiedHead.revision)
        : null;
      if (!verified) return verificationFailure("冲突副本 Head 或 Digest 核验失败");
      lastCommitted.set(copyId, verified);
      lastCommittedRevision.set(copyId, verifiedHead?.revision as string);
      if (!finalizeConflictRecord(storage, record, "resolved-copy")) {
        return verificationFailure("冲突副本已提交，但原分支释放标记核验失败");
      }
      return { ok: true, copy: verified };
    };
    return arbitrate(() => transaction("exclusive"), () => transaction("proposal"));
  }

  function resolveConflictByReloadArbitrated(
    conversationId: string,
    conflictId: string,
    storage: StorageLike | null = browserStorage(),
  ): Promise<{ ok: true; conversation: Conversation; revision: string } | PersistenceFlushFailure> {
    if (!storage) return Promise.resolve(storageUnavailableFailure("localStorage 不可用"));
    const transaction = (): { ok: true; conversation: Conversation; revision: string } | PersistenceFlushFailure => {
      const record = parseDurableConflictBranch(safeGetItem(storage, sessionConflictKeyV3(conversationId, conflictId)));
      if (!record) return verificationFailure("冲突记录不存在或已损坏");
      const shared = readSharedConversation(conversationId, storage);
      if (!shared) return verificationFailure("共享 Head 核验失败");
      lastCommitted.set(conversationId, shared.conversation);
      lastCommittedRevision.set(conversationId, shared.revision);
      if (!finalizeConflictRecord(storage, record, "discarded")) {
        return verificationFailure("共享 Head 已读取，但冲突丢弃标记核验失败");
      }
      return { ok: true, conversation: shared.conversation, revision: shared.revision };
    };
    return arbitrate(transaction, transaction);
  }

  function adoptRemoteConversation(conversationId: string, conversation: Conversation, revision: string): void {
    lastCommitted.set(conversationId, conversation);
    lastCommittedRevision.set(conversationId, revision);
  }

  /** 当前仍处于脏状态的会话：identity 未登记为已提交、非空、未被 tombstone 拒绝。 */
  function collectDirtyConversations(state: PersistedConversationState): Conversation[] {
    return state.conversations.filter((conversation) =>
      conversation.messages.length > 0
      && lastCommitted.get(conversation.id) !== conversation
      && !tombstonedRefused.has(conversation.id));
  }

  function buildRecoveryCapsule(
    writerSessionId: string,
    sequence: number,
    dirty: Conversation[],
    level: 0 | 1 | 2,
  ): RecoveryCapsule {
    const entries = dirty.map((conversation): RecoveryCapsuleEntry => {
      const compacted = level === 0
        ? { conversation, removedPreviewBytes: 0 }
        : compactConversationForStorage(conversation, level);
      const normalized = normalizeConversationForCommit(compacted.conversation);
      return {
        conversationId: conversation.id,
        baseRevision: lastCommittedRevision.get(conversation.id) ?? null,
        digest: checkpointDigest(JSON.stringify(normalized)),
        conversation: normalized,
        ...(level > 0 && compacted.removedPreviewBytes > 0
          ? { compaction: { level, removedPreviewBytes: compacted.removedPreviewBytes, reason: "storage-pressure" } }
          : {}),
      };
    });
    const unsigned = {
      schemaVersion: 2 as const,
      writerSessionId,
      sequence,
      savedAt: Date.now(),
      entries,
    };
    return { ...unsigned, digest: checkpointDigest(JSON.stringify(unsigned)) };
  }

  /** V2 胶囊按原始→两级确定性压缩重试，每次写入都必须逐字节回读。 */
  function writeCapsuleRecord(storage: StorageLike, writerSessionId: string, dirty: Conversation[]): PersistenceFlushFailure | null {
    const existing = parseRecoveryCapsule(safeGetItem(storage, sessionRecoveryKeyV3(writerSessionId)));
    capsuleSequence = Math.max(capsuleSequence + 1, (existing?.sequence ?? 0) + 1);
    let lastQuotaFailure: PersistenceFlushFailure | null = null;
    for (const level of [0, 1, 2] as const) {
      let serialized: string;
      try {
        serialized = JSON.stringify(buildRecoveryCapsule(writerSessionId, capsuleSequence, dirty, level));
      } catch (error) {
        return classifyStorageError(error);
      }
      try {
        const key = sessionRecoveryKeyV3(writerSessionId);
        storage.setItem(key, serialized);
        if (storage.getItem(key) !== serialized) {
          return withSizeHint(verificationFailure("恢复胶囊写入后回读核验失败"), serialized.length);
        }
        return null;
      } catch (error) {
        const classified = classifyStorageError(error);
        const failure = withSizeHint({ ...classified, message: `恢复胶囊写入失败：${classified.message}` }, serialized.length);
        if (classified.code !== "quota-exceeded") return failure;
        lastQuotaFailure = failure;
      }
    }
    return { ...(lastQuotaFailure ?? storageUnavailableFailure("恢复胶囊写入失败")), code: "storage-pressure" };
  }

  /**
   * 胶囊保鲜（best-effort）：仅在胶囊键存在时动手——脏集合耗尽 ⇒ 移除；
   * 部分提交 ⇒ 以剩余脏重写。写 / 删失败留下过期胶囊，下次启动对账时收敛。
   */
  function refreshRecoveryCapsule(storage: StorageLike, writerSessionId: string, state: PersistedConversationState): void {
    const key = sessionRecoveryKeyV3(writerSessionId);
    if (safeGetItem(storage, key) === null) return;
    const dirty = collectDirtyConversations(state);
    if (!dirty.length) {
      try {
        storage.removeItem(key);
      } catch {
        // 残留胶囊重处理时收敛，无害。
      }
      return;
    }
    writeCapsuleRecord(storage, writerSessionId, dirty);
  }

  function writeRecoveryCapsule(
    state: PersistedConversationState,
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
  ): PersistenceFlushFailure | null {
    if (!storage) return null; // 存储不可用：flush 本身已按 storage-unavailable 上报。
    const writerSessionId = getReplicaIdentity(session).writerSessionId;
    const dirty = collectDirtyConversations(state);
    if (!dirty.length) {
      // flush 成功 ⇒ 空集合 ⇒ 移除胶囊键；删失败留下过期胶囊，对账时收敛。
      try {
        storage.removeItem(sessionRecoveryKeyV3(writerSessionId));
      } catch {
        // best-effort。
      }
      return null;
    }
    return writeCapsuleRecord(storage, writerSessionId, dirty);
  }

  /**
   * 对账单个胶囊条目（调用方已持有排他锁）。返回 true 表示条目已耐久处理
   * （补交落盘 / 副本落盘 / 收敛跳过）；提交失败返回 false 并记入 outcome.failed，
   * 胶囊因此保留，下次启动幂等重试。
   */
  function reconcileCapsuleEntry(
    storage: StorageLike,
    writerSessionId: string,
    capsule: RecoveryCapsule,
    entry: RecoveryCapsuleEntry,
    outcome: RecoveryReconcileOutcome,
    options?: SaveConversationOptions,
    mode: "exclusive" | "proposal" = "exclusive",
  ): boolean {
    const migrated = migrateLegacyConversation(entry.conversation);
    // 无法迁移的条目本就无法加载，按已处理（绝不阻塞其余条目与胶囊删除）。
    if (!migrated) return true;
    const conversation: Conversation = { ...migrated, messages: migrated.messages.map(checkpointMessage) };
    const conversationId = conversation.id;
    const base = entry.baseRevision;
    const previous = lastCommitted.get(conversationId) ?? null;
    const head = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(conversationId)));
    const tombstoned = safeGetItem(storage, sessionTombstoneKeyV3(conversationId)) !== null;

    const registerCommitted = (revision: string): void => {
      lastCommitted.set(conversationId, conversation);
      lastCommittedRevision.set(conversationId, revision);
    };

    if (!tombstoned) {
      // 收敛检查：head 内容已与胶囊条目一致（此前的补交已落地，或他人提交了
      // 完全相同的内容）⇒ 不重复提交，按已补交登记。
      if (head?.digest) {
        let entryDigest: string | null = null;
        try {
          entryDigest = checkpointDigest(JSON.stringify(normalizeConversationForCommit(conversation)));
        } catch {
          entryDigest = null;
        }
        if (entryDigest && entryDigest === head.digest) {
          registerCommitted(head.revision);
          outcome.committed.push({ conversationId, conversation, revision: head.revision, previous });
          return true;
        }
      }
      // 干净路径：共享 head 缺失（无 tombstone）或仍停留在 base ⇒ 作为正常分片提交补交。
      if ((!head && base === null) || head?.revision === base) {
        if (base) lastCommittedRevision.set(conversationId, base);
        else lastCommittedRevision.delete(conversationId);
        const committed = commitConversationShard(storage, conversation, writerSessionId, mode);
        if (!committed.ok) {
          outcome.failed.push(committed);
          return false;
        }
        registerCommitted(committed.revision);
        options?.onCommit?.({ conversationId, revision: committed.revision, writerId: writerSessionId, savedAt: committed.savedAt });
        // 锁退化期间 head 被并发推进：分支已耐久落盘，按冲突信号带外上报（数据安全）。
        if (committed.kind === "conflict") {
          options?.onConflict?.({
            conversationId,
            conflictId: committed.conflictId,
            title: conversation.title,
            revision: committed.revision,
            baseRevision: committed.baseRevision,
            sharedRevision: committed.sharedRevision,
            writerId: writerSessionId,
            savedAt: committed.savedAt,
          });
          return true;
        }
        outcome.committed.push({ conversationId, conversation, revision: committed.revision, previous });
        return true;
      }
    }

    // head 已被兄弟标签页推进，或 cid 已被 tombstone 覆盖 ⇒ 以确定性 id 物化
    // 恢复副本（标题加"（恢复副本）"后缀），作为它自己的分片提交。
    const copy: Conversation = {
      ...conversation,
      id: recoveredCopyIdV3(conversationId, capsule.writerSessionId, capsule.sequence),
      title: `${conversation.title}（恢复副本）`,
      customTitle: true,
      favorite: false,
    };
    // 同一胶囊重处理：副本 head 已存在 ⇒ 此前的对账已提交它，登记映射并收敛（at most once）。
    const copyHead = parseV3Head(safeGetItem(storage, sessionHeadKeyV3(copy.id)));
    if (copyHead) {
      const expectedDigest = checkpointDigest(JSON.stringify(normalizeConversationForCommit(copy)));
      if (copyHead.digest !== expectedDigest) {
        outcome.failed.push(conflictFailure("恢复副本 Head 与胶囊摘要不一致", conversationId, estimateConversationBytes(copy)));
        return false;
      }
      lastCommitted.set(copy.id, copy);
      lastCommittedRevision.set(copy.id, copyHead.revision);
      outcome.recovered.push(copy);
      return true;
    }
    lastCommittedRevision.delete(copy.id);
    const copyCommit = commitConversationShard(storage, copy, writerSessionId, mode);
    if (!copyCommit.ok) {
      outcome.failed.push(copyCommit);
      return false;
    }
    lastCommitted.set(copy.id, copy);
    lastCommittedRevision.set(copy.id, copyCommit.revision);
    options?.onCommit?.({ conversationId: copy.id, revision: copyCommit.revision, writerId: writerSessionId, savedAt: copyCommit.savedAt });
    outcome.recovered.push(copy);
    return true;
  }

  /** 处理一份胶囊的全部条目；全部耐久处理后才删除胶囊键（at most once 的另一半）。 */
  function reconcileCapsule(
    storage: StorageLike,
    writerSessionId: string,
    key: string,
    capsule: RecoveryCapsule,
    outcome: RecoveryReconcileOutcome,
    options?: SaveConversationOptions,
    mode: "exclusive" | "proposal" = "exclusive",
  ): void {
    let allHandled = true;
    for (const entry of capsule.entries) {
      if (!reconcileCapsuleEntry(storage, writerSessionId, capsule, entry, outcome, options, mode)) allHandled = false;
    }
    if (!allHandled) return;
    const markerKey = `${conversationStorageKeys.v3RecoveryResolvedPrefix}${capsule.writerSessionId}.${capsule.sequence}`;
    const marker = JSON.stringify({
      schemaVersion: 1,
      writerSessionId: capsule.writerSessionId,
      sequence: capsule.sequence,
      capsuleDigest: capsule.digest,
      resolvedAt: Date.now(),
    });
    try {
      storage.setItem(markerKey, marker);
      if (storage.getItem(markerKey) !== marker) {
        outcome.failed.push(verificationFailure("恢复胶囊 resolved marker 核验失败"));
        return;
      }
      storage.removeItem(key);
    } catch {
      // 删除失败：胶囊重处理时经 digest / 副本 head 检查收敛。
    }
  }

  function quarantineInvalidCapsule(storage: StorageLike, key: string, raw: string, outcome: RecoveryReconcileOutcome): void {
    const owner = key.slice(conversationStorageKeys.v3RecoveryPrefix.length) || "unknown";
    const quarantineKey = `${conversationStorageKeys.v3QuarantinePrefix}capsule.${owner}.${Date.now()}`;
    try {
      storage.setItem(quarantineKey, raw);
      if (storage.getItem(quarantineKey) !== raw) {
        outcome.failed.push(verificationFailure("损坏恢复胶囊 quarantine 核验失败"));
        return;
      }
      storage.removeItem(key);
      outcome.failed.push(verificationFailure("检测到损坏恢复胶囊，已隔离"));
    } catch (error) {
      outcome.failed.push(classifyStorageError(error));
    }
  }

  function reconcileRecoveryCapsules(
    storage: StorageLike | null = browserStorage(),
    session: StorageLike | null = browserSessionStorage(),
    options?: SaveConversationOptions,
  ): Promise<RecoveryReconcileOutcome> {
    const outcome: RecoveryReconcileOutcome = { committed: [], recovered: [], failed: [] };
    if (!storage) return Promise.resolve(outcome);
    const writerSessionId = getReplicaIdentity(session).writerSessionId;
    const reconcileAll = (mode: "exclusive" | "proposal"): RecoveryReconcileOutcome => {
      // 本标签页胶囊（崩溃/杀死但会话恢复，sessionStorage 幸存）：无租约条件直接对账。
      const ownKey = sessionRecoveryKeyV3(writerSessionId);
      const ownRaw = safeGetItem(storage, ownKey);
      const own = parseRecoveryCapsule(ownRaw);
      if (own) reconcileCapsule(storage, writerSessionId, ownKey, own, outcome, options, mode);
      else if (ownRaw) quarantineInvalidCapsule(storage, ownKey, ownRaw, outcome);
      // 孤儿胶囊（属主已崩溃的新标签页拿不到原 tabId）：仅当属主租约缺失或
      // 已过期（>5min 未触碰，与 tombstone 租约同规）才回收；活租约留给属主
      // （BFCache 恢复情形，属主经胶囊保鲜规则自行清理）。预算有界，剩余下轮再收。
      const keys = enumerateKeysWithPrefix(storage, conversationStorageKeys.v3RecoveryPrefix) ?? [];
      const now = Date.now();
      let budget = FOREIGN_RECOVERY_CAPSULE_BUDGET;
      for (const key of keys) {
        if (budget <= 0) break;
        if (key === ownKey) continue;
        const ownerWriterSessionId = key.slice(conversationStorageKeys.v3RecoveryPrefix.length);
        if (!ownerWriterSessionId) continue;
        const leaseLastSeen = readJsonNumber(safeGetItem(storage, `${conversationStorageKeys.v3TabPrefix}${ownerWriterSessionId}`), "lastSeen");
        if (leaseLastSeen && now - leaseLastSeen <= CONVERSATION_TOMBSTONE_LIMITS.tabLeaseStaleMs) continue;
        budget -= 1;
        const raw = safeGetItem(storage, key);
        const capsule = parseRecoveryCapsule(raw);
        if (!capsule) {
          if (raw) quarantineInvalidCapsule(storage, key, raw, outcome);
          continue;
        }
        reconcileCapsule(storage, writerSessionId, key, capsule, outcome, options, mode);
      }
      return outcome;
    };
    return arbitrate(() => reconcileAll("exclusive"), () => reconcileAll("proposal"));
  }

  function reset(): void {
    lastCommitted.clear();
    lastCommittedRevision.clear();
    localConflictBranches.clear();
    degradedHeads.clear();
    lastProposalKey.clear();
    lastSettledProposalFingerprint.clear();
    pendingTombstones.clear();
    tombstonedRefused.clear();
    lastHeadRevision = null;
    idleGcScheduled = false;
    capsuleSequence = 0;
    identity = null;
  }

  return {
    load,
    save,
    saveArbitrated,
    reconcileRemoteCommit,
    persistSelection,
    deleteConversationArbitrated,
    setTabLease,
    readSharedConversation,
    readConflictBranch,
    listConflictBranches,
    clearConflict,
    resolveConflictByCopyArbitrated,
    resolveConflictByReloadArbitrated,
    getReplicaIdentity,
    rotateWriterIdentity,
    adoptRemoteConversation,
    writeRecoveryCapsule,
    reconcileRecoveryCapsules,
    reset,
  };
}

const defaultAdapter = createConversationPersistenceAdapter({ legacyWriterFromContinuity: true });

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
  conflictId?: string,
): { pointer: ConversationConflictPointer; branch: DurableConflictBranch; conversation: Conversation } | null {
  return defaultAdapter.readConflictBranch(conversationId, storage, conflictId);
}

export function resetConversationPersistenceForTests(): void {
  defaultAdapter.reset();
}
