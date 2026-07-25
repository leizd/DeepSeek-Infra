import type { ChatMessage } from "../chat/types";
import {
  classifyStorageError,
  storageUnavailableFailure,
  verificationFailure,
  type PersistenceFlushFailure,
} from "../../app/persistenceErrors";
import type { PersistenceFlushResult } from "../../app/reloadBlockers";
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

export const conversationStorageKeys = {
  conversations: "deepseek-infra.conversations",
  currentConversation: "deepseek-infra.current-conversation",
  legacyMessages: "deepseek-infra.messages",
  sessionHead: "deepseek-infra.session.v2.head",
} as const;

export function sessionSnapshotKey(generation: number): string {
  return `deepseek-infra.session.v2.snapshot.${generation}`;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function browserStorage(): StorageLike | null {
  return typeof window === "undefined" ? null : window.localStorage;
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
  try {
    const raw = storage.getItem(conversationStorageKeys.sessionHead);
    if (!raw) return 0;
    const generation = Number.parseInt(raw, 10);
    return Number.isFinite(generation) && generation > 0 ? generation : 0;
  } catch {
    return 0;
  }
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
  let raw: string | null;
  try {
    raw = storage.getItem(sessionSnapshotKey(generation));
  } catch {
    return null;
  }
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

export function loadPersistedConversationState(storage: StorageLike | null = browserStorage()): PersistedConversationState {
  if (!storage) return { schemaVersion: 1, currentConversationId: null, conversations: [] };
  // V2 journal 优先：head generation → 损坏时回退 head-1 → 都不可用才走 legacy。
  const head = readHeadGeneration(storage);
  for (const generation of [head, head - 1]) {
    if (generation < 1) continue;
    const checkpoint = loadCheckpoint(storage, generation);
    if (checkpoint) return checkpoint;
  }
  let conversations = normalizeConversations(parseArray(storage.getItem(conversationStorageKeys.conversations)));

  if (!conversations.length) {
    const messages = parseArray(storage.getItem(conversationStorageKeys.legacyMessages))
      .map(migrateLegacyMessage)
      .filter((message): message is ChatMessage => Boolean(message));
    if (messages.length) {
      conversations = [createConversation(createId("legacy-conversation"), messages.map(checkpointMessage), DEFAULT_MODEL, true)];
    }
  }

  conversations = sortConversations(conversations);
  const requestedId = storage.getItem(conversationStorageKeys.currentConversation);
  return { schemaVersion: 1, currentConversationId: selectCurrentConversationId(conversations, requestedId), conversations };
}

function normalizeForCommit(state: PersistedConversationState): Conversation[] {
  return sortConversations(state.conversations)
    .filter((conversation) => conversation.messages.length)
    .map((conversation) => ({
      ...conversation,
      messages: conversation.messages.slice(-80).map(checkpointMessage),
    }));
}

function buildCheckpoint(state: PersistedConversationState, generation: number): ConversationCheckpointV2 {
  return {
    schemaVersion: 2,
    generation,
    savedAt: Date.now(),
    currentConversationId: state.currentConversationId,
    conversations: normalizeForCommit(state),
  };
}

/** 估算 state 序列化为 checkpoint 后的 UTF-8 字节数，用于失败结果里的负载提示。 */
export function estimateCheckpointBytes(state: PersistedConversationState): number {
  try {
    return new TextEncoder().encode(JSON.stringify(buildCheckpoint(state, 0))).length;
  } catch {
    return 0;
  }
}

function withSizeHint(failure: PersistenceFlushFailure, bytes: number): PersistenceFlushFailure {
  return { ...failure, message: `${failure.message} (~${bytes} bytes)` };
}

/**
 * 以 generation journal 原子提交会话状态：
 *   snapshot(N) 写入 → 回读核验 → head 指针推进到 N → 清理 < N-1 的旧快照
 *   → 最后才删除 legacy conversations / currentConversation 键。
 * head 推进之前的任何失败都不会动到最后一次已提交的 checkpoint；legacy 键
 * 只在首次 V2 提交成功后删除（legacyMessages 比本方案更老，保持原有语义不删）。
 * 任何路径都不抛异常，失败以 PersistenceFlushResult 返回并附估算负载大小。
 */
export function savePersistedConversationState(
  state: PersistedConversationState,
  storage: StorageLike | null = browserStorage(),
): PersistenceFlushResult {
  const bytes = estimateCheckpointBytes(state);
  if (!storage) return withSizeHint(storageUnavailableFailure("localStorage 不可用"), bytes);
  const generation = readHeadGeneration(storage) + 1;
  let serialized: string;
  try {
    serialized = JSON.stringify(buildCheckpoint(state, generation));
  } catch (error) {
    return withSizeHint(classifyStorageError(error), bytes);
  }
  const snapshotKey = sessionSnapshotKey(generation);
  try {
    storage.setItem(snapshotKey, serialized);
  } catch (error) {
    return withSizeHint(classifyStorageError(error), bytes);
  }
  // 写入后回读核验：读不到或内容不一致都视为失败，head 保持不动。
  let stored: string | null;
  try {
    stored = storage.getItem(snapshotKey);
  } catch (error) {
    return withSizeHint(classifyStorageError(error), bytes);
  }
  if (stored !== serialized) {
    return withSizeHint(verificationFailure("会话快照写入后回读内容不一致"), bytes);
  }
  try {
    storage.setItem(conversationStorageKeys.sessionHead, String(generation));
  } catch (error) {
    // snapshot N 可能残留，但它不被 head 引用——下次提交会覆盖或由清理回收。
    return withSizeHint(classifyStorageError(error), bytes);
  }
  // 提交成功：只保留 N 与 N-1 两份快照。
  for (let stale = generation - 2; stale >= 1; stale -= 1) {
    try {
      storage.removeItem(sessionSnapshotKey(stale));
    } catch {
      // 清理失败无害：残留快照不被引用，下次提交会重试清理。
    }
  }
  // V2 checkpoint 已核验提交，此刻才允许删除 legacy 键。
  for (const key of [conversationStorageKeys.conversations, conversationStorageKeys.currentConversation]) {
    try {
      storage.removeItem(key);
    } catch {
      // 残留 legacy 键只会让 v1 读取器看到旧数据，V2 提交本身已完成。
    }
  }
  return { ok: true, revision: String(generation) };
}
