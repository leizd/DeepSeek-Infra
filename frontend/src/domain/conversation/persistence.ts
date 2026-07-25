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
import { createConversation, sortConversations } from "./reducer";
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

export const conversationStorageKeys = {
  conversations: "deepseek-infra.conversations",
  currentConversation: "deepseek-infra.current-conversation",
  legacyMessages: "deepseek-infra.messages",
  sessionHead: "deepseek-infra.session.v2.head",
  currentConversationV3: "deepseek-infra.current-conversation.v3",
  v3HeadPrefix: "deepseek-infra.session.v3.head.",
  v3SnapshotPrefix: "deepseek-infra.session.v3.snapshot.",
  v3TombstonePrefix: "deepseek-infra.session.v3.tombstone.",
  v3RecoveryPrefix: "deepseek-infra.session.v3.recovery.",
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

export function sessionTombstoneKeyV3(conversationId: string): string {
  return `${conversationStorageKeys.v3TombstonePrefix}${conversationId}`;
}

export function sessionRecoveryKeyV3(tabId: string): string {
  return `${conversationStorageKeys.v3RecoveryPrefix}${tabId}`;
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

/**
 * 枚举所有 V3 head，逐会话回读：head.revision 快照优先，损坏/缺失时回退
 * parentRevision。遇到 tombstone 的会话直接跳过（写入方在后续提交落地）。
 * 存储不支持枚举时返回 null，调用方继续走 V2 / legacy 读取链。
 */
function loadV3Conversations(storage: StorageLike): Conversation[] | null {
  const headKeys = enumerateKeysWithPrefix(storage, conversationStorageKeys.v3HeadPrefix);
  if (!headKeys?.length) return null;
  const conversations: Conversation[] = [];
  for (const headKey of headKeys) {
    const conversationId = headKey.slice(conversationStorageKeys.v3HeadPrefix.length);
    if (!conversationId) continue;
    if (safeGetItem(storage, sessionTombstoneKeyV3(conversationId)) !== null) continue;
    const head = parseV3Head(safeGetItem(storage, headKey));
    if (!head) continue;
    const conversation = loadV3SnapshotConversation(storage, conversationId, head.revision)
      ?? (head.parentRevision ? loadV3SnapshotConversation(storage, conversationId, head.parentRevision) : null);
    if (conversation) conversations.push(conversation);
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

export function loadPersistedConversationState(
  storage: StorageLike | null = browserStorage(),
  session: StorageLike | null = browserSessionStorage(),
): PersistedConversationState {
  if (!storage) return { schemaVersion: 1, currentConversationId: null, conversations: [] };
  // V3 分片优先；其后依次为 V2 journal（迁移读取器，保持原逻辑）与 legacy 键。
  const sharded = loadV3Conversations(storage);
  if (sharded) {
    const conversations = sortConversations(sharded);
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

function normalizeConversationForCommit(conversation: Conversation): Conversation {
  return {
    ...conversation,
    messages: conversation.messages.slice(-80).map(checkpointMessage),
  };
}

function normalizeForCommit(state: PersistedConversationState): Conversation[] {
  return sortConversations(state.conversations)
    .filter((conversation) => conversation.messages.length)
    .map(normalizeConversationForCommit);
}

/** 估算单个会话序列化后的 UTF-8 字节数，用于分片失败结果里的负载提示。 */
export function estimateConversationBytes(conversation: Conversation): number {
  try {
    return new TextEncoder().encode(JSON.stringify(normalizeConversationForCommit(conversation))).length;
  } catch {
    return 0;
  }
}

/** 估算整份 state 规范化后的 UTF-8 字节数（存储整体不可用时的负载提示）。 */
export function estimateCheckpointBytes(state: PersistedConversationState): number {
  try {
    return new TextEncoder().encode(JSON.stringify(normalizeForCommit(state))).length;
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
 * 持久化适配器状态。chat reducer 只替换发生变化的会话对象（其余保持对象
 * identity），因此 identity 对比即可圈出脏分片：一次会话变更只序列化该会话。
 */
const lastCommitted = new Map<string, Conversation>();
const lastCommittedRevision = new Map<string, string>();
let lastHeadRevision: string | null = null;
let idleGcScheduled = false;

export function resetConversationPersistenceForTests(): void {
  lastCommitted.clear();
  lastCommittedRevision.clear();
  lastHeadRevision = null;
  idleGcScheduled = false;
}

type ShardCommitResult = { ok: true; revision: string } | PersistenceFlushFailure;

function storedCheckpointMatches(stored: string | null, conversationId: string, revision: string, digest: string): boolean {
  if (!stored) return false;
  try {
    const parsed = JSON.parse(stored) as Partial<ConversationCheckpointV3> | null;
    return Boolean(
      parsed
      && parsed.schemaVersion === 3
      && parsed.conversationId === conversationId
      && parsed.revision === revision
      && parsed.digest === digest,
    );
  } catch {
    return false;
  }
}

/**
 * 提交单个会话分片：snapshot 写入 → 回读核验 digest + revision → head 推进。
 * head 推进之前的任何失败都不动已提交的上一份 checkpoint；head 写失败则
 * 快照成为孤儿，由保留/空闲 GC 回收。任何路径都不抛异常。
 */
function commitConversationShard(storage: StorageLike, conversation: Conversation, tabId: string): ShardCommitResult {
  const bytes = estimateConversationBytes(conversation);
  let headRaw: string | null;
  try {
    headRaw = storage.getItem(sessionHeadKeyV3(conversation.id));
  } catch (error) {
    return shardFailure(classifyStorageError(error), conversation.id, bytes);
  }
  const head = parseV3Head(headRaw);
  const parentRevision = head?.revision ?? null;
  const revision = `${revisionSeq(parentRevision) + 1}.${tabId}`;
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
    return shardFailure(classifyStorageError(error), conversation.id, bytes);
  }
  const snapshotKey = sessionSnapshotKeyV3(conversation.id, revision);
  try {
    storage.setItem(snapshotKey, serialized);
  } catch (error) {
    return shardFailure(classifyStorageError(error), conversation.id, bytes);
  }
  let stored: string | null;
  try {
    stored = storage.getItem(snapshotKey);
  } catch (error) {
    return shardFailure(classifyStorageError(error), conversation.id, bytes);
  }
  if (!storedCheckpointMatches(stored, conversation.id, revision, digest)) {
    return shardFailure(verificationFailure("快照写入后回读核验失败"), conversation.id, bytes);
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
  // 保留式 GC：有界 O(1)——最多删除 2 份既非当前也非 parentRevision 的旧快照。
  collectStaleSnapshots(storage, conversation.id, new Set(parentRevision ? [revision, parentRevision] : [revision]), 2);
  return { ok: true, revision };
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

/**
 * 硬删除一个已从 state 消失的会话：head + 至多 2 份快照（tombstone 语义由
 * 下一提交接管，此处保持与整存时代 observable-equal 的删除行为）。
 */
function deleteConversationShard(storage: StorageLike, conversationId: string): void {
  const knownRevision = lastCommittedRevision.get(conversationId);
  let removedSnapshots = 0;
  let failed = false;
  try {
    storage.removeItem(sessionHeadKeyV3(conversationId));
  } catch {
    failed = true;
  }
  if (knownRevision) {
    try {
      storage.removeItem(sessionSnapshotKeyV3(conversationId, knownRevision));
      removedSnapshots += 1;
    } catch {
      failed = true;
    }
  }
  const prefix = sessionSnapshotKeyV3(conversationId, "");
  const keys = enumerateKeysWithPrefix(storage, prefix);
  if (keys) {
    for (const key of keys) {
      if (removedSnapshots >= 2) break;
      if (knownRevision && key === sessionSnapshotKeyV3(conversationId, knownRevision)) continue;
      try {
        storage.removeItem(key);
        removedSnapshots += 1;
      } catch {
        failed = true;
      }
    }
  }
  // 删除失败则保留适配器记录，下一次 flush 重试。
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
  let hasMigratedKeys = false;
  for (const key of [conversationStorageKeys.sessionHead, conversationStorageKeys.conversations, conversationStorageKeys.currentConversation]) {
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
  for (const key of [
    conversationStorageKeys.sessionHead,
    conversationStorageKeys.conversations,
    conversationStorageKeys.currentConversation,
    ...v2SnapshotKeys,
  ]) {
    try {
      storage.removeItem(key);
    } catch {
      // 残留键只会让旧读取器看到过期数据，V3 提交本身已完成。
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
 * 空闲孤儿 GC：扫描所有 V3 快照，删除不被所属会话 head / parentRevision
 * 引用的快照（崩溃/失败写入的残留），单次最多 budget 份，返回删除数。
 * 存储不支持枚举时直接返回 0。
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
  return removed;
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
  if (typeof requestIdle === "function") requestIdle(run);
  else setTimeout(run, 0);
}

/**
 * 以会话为分片提交会话状态：
 *   删除已从 state 消失的会话分片 → 逐个脏会话 snapshot 写入 → 回读核验
 *   → head 推进 → 有界保留 GC → 全部脏分片成功后清理 V2 / legacy 键
 *   → 调度一次空闲孤儿 GC。
 * 当前会话选中只写入本标签页的 sessionStorage，绝不进入 V3 共享键。
 * 任何路径都不抛异常，失败以 PersistenceFlushResult 返回并附估算负载大小。
 */
export function savePersistedConversationState(
  state: PersistedConversationState,
  storage: StorageLike | null = browserStorage(),
  session: StorageLike | null = browserSessionStorage(),
): PersistenceFlushResult {
  if (!storage) return withSizeHint(storageUnavailableFailure("localStorage 不可用"), estimateCheckpointBytes(state));
  const tabId = getTabId(session);
  writeTabSelection(session, state.currentConversationId);

  const present = new Set(state.conversations.map((conversation) => conversation.id));
  for (const conversationId of [...lastCommitted.keys()]) {
    if (!present.has(conversationId)) deleteConversationShard(storage, conversationId);
  }

  for (const conversation of state.conversations) {
    if (!conversation.messages.length) continue;
    if (lastCommitted.get(conversation.id) === conversation) continue;
    const result = commitConversationShard(storage, conversation, tabId);
    if (!result.ok) return result;
    lastCommitted.set(conversation.id, conversation);
    lastCommittedRevision.set(conversation.id, result.revision);
    lastHeadRevision = result.revision;
  }

  // 此刻 state 中所有会话都已核验落入 V3，才允许清理 V2 / legacy 键。
  cleanupMigratedKeys(storage);
  scheduleIdleCheckpointGc(storage);
  return lastHeadRevision ? { ok: true, revision: lastHeadRevision } : { ok: true };
}
