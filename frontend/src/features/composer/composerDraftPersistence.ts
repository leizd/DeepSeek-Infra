import {
  classifyStorageError,
  storageUnavailableFailure,
  verificationFailure,
  type PersistenceFlushFailure,
} from "../../app/persistenceErrors";
import type { PersistenceFlushResult } from "../../app/reloadBlockers";

export interface ComposerDraft {
  conversationId: string;
  projectId?: string | null;
  text: string;
  updatedAt: number;
}

export interface ComposerDraftScope {
  conversationId: string;
  projectId: string | null;
}

export interface SessionStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const KEY_PREFIX = "deepseek:composer-draft:";

function browserSessionStorage(): SessionStorageLike | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function composerDraftStorageKey(scope: ComposerDraftScope): string {
  return `${KEY_PREFIX}${encodeURIComponent(scope.conversationId)}:${encodeURIComponent(scope.projectId ?? "")}`;
}

function legacyDraftStorageKey(conversationId: string): string {
  return `${KEY_PREFIX}${conversationId}`;
}

function parseDraft(raw: string, conversationId: string): ComposerDraft | null {
  try {
    const value = JSON.parse(raw) as Partial<ComposerDraft>;
    if (
      value.conversationId !== conversationId
      || typeof value.text !== "string"
      || typeof value.updatedAt !== "number"
      || (value.projectId !== undefined && value.projectId !== null && typeof value.projectId !== "string")
    ) {
      return null;
    }
    return {
      conversationId,
      projectId: value.projectId ?? null,
      text: value.text,
      updatedAt: value.updatedAt,
    };
  } catch {
    return null;
  }
}

function readStoredDraft(
  storage: SessionStorageLike,
  key: string,
  scope: ComposerDraftScope,
): ComposerDraft | null {
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    const draft = parseDraft(raw, scope.conversationId);
    if (!draft || (draft.projectId ?? null) !== scope.projectId) return null;
    return { ...draft, projectId: scope.projectId };
  } catch {
    return null;
  }
}

/**
 * 写失败时细分失败码：配额与安全错误由 classifyStorageError 直接归类；
 * 其余错误若连读取也抛错，说明存储整体被禁用，归 storage-unavailable
 * 而不是 unknown。
 */
function classifyWriteFailure(
  storage: SessionStorageLike,
  key: string,
  error: unknown,
): PersistenceFlushFailure {
  const classified = classifyStorageError(error);
  if (classified.code !== "unknown") return classified;
  try {
    storage.getItem(key);
  } catch {
    return storageUnavailableFailure(classified.message);
  }
  return classified;
}

/**
 * 旧版草稿只按会话保存。第一次读取时把它迁移到会话 + 项目的作用域键：
 * 草稿里带有项目 ID 就归入该项目，无法确定时归入无项目（none），
 * 绝不自动绑定当前项目。
 *
 * 迁移是无损的：先写入新键并回读核验内容一致，确认无误后才删除旧键。
 * 写入抛错或回读核验不一致时旧键原样保留，草稿仍可从旧键加载（见
 * loadComposerDraft 的旧键兜底）。返回 null 表示没有旧版草稿；
 * 否则返回迁移的 PersistenceFlushResult（ok:true 含草稿 revision）。
 */
export function migrateLegacyDraft(
  conversationId: string,
  storage: SessionStorageLike,
): PersistenceFlushResult | null {
  const legacyKey = legacyDraftStorageKey(conversationId);
  let raw: string | null;
  try {
    raw = storage.getItem(legacyKey);
  } catch {
    return null;
  }
  if (raw === null) return null;
  const draft = parseDraft(raw, conversationId);
  if (!draft || !draft.text) {
    // 内容无效或为空：没有可丢失的草稿，旧键只清理一次。
    try {
      storage.removeItem(legacyKey);
    } catch {
      // 清理失败也无妨：无效内容下次仍会走到这里。
    }
    return { ok: true };
  }
  const saved = saveComposerDraft(draft, storage);
  if (!saved.ok) {
    // 新键未通过写入/核验，旧键必须保留——草稿唯一的完整副本还在那里。
    return saved;
  }
  try {
    storage.removeItem(legacyKey);
  } catch {
    // 新键已核验，旧键残留只会导致下次幂等地再迁移一次，不会丢草稿。
  }
  return saved;
}

export function loadComposerDraft(
  scope: ComposerDraftScope,
  storage: SessionStorageLike | null = browserSessionStorage(),
): ComposerDraft | null {
  if (!storage) return null;
  migrateLegacyDraft(scope.conversationId, storage);
  const scoped = readStoredDraft(storage, composerDraftStorageKey(scope), scope);
  if (scoped) return scoped;
  // 迁移失败（配额 / 存储被禁用 / 核验不一致）时旧键仍在：从旧键兜底加载。
  return readStoredDraft(storage, legacyDraftStorageKey(scope.conversationId), scope);
}

export function saveComposerDraft(
  draft: ComposerDraft,
  storage: SessionStorageLike | null = browserSessionStorage(),
): PersistenceFlushResult {
  const revision = String(draft.updatedAt);
  if (!storage) return storageUnavailableFailure("sessionStorage 不可用");
  const key = composerDraftStorageKey({
    conversationId: draft.conversationId,
    projectId: draft.projectId ?? null,
  });
  if (!draft.text) {
    try {
      storage.removeItem(key);
      return { ok: true, revision };
    } catch (error) {
      return classifyWriteFailure(storage, key, error);
    }
  }
  const serialized = JSON.stringify(draft);
  try {
    storage.setItem(key, serialized);
  } catch (error) {
    return classifyWriteFailure(storage, key, error);
  }
  // 写入后回读核验：读不到或内容不一致都视为 verification-failed。
  let stored: string | null;
  try {
    stored = storage.getItem(key);
  } catch (error) {
    return classifyStorageError(error);
  }
  if (stored !== serialized) {
    return verificationFailure("草稿写入后回读内容不一致");
  }
  return { ok: true, revision };
}

export function clearComposerDraft(
  scope: ComposerDraftScope,
  storage: SessionStorageLike | null = browserSessionStorage(),
): PersistenceFlushResult {
  if (!storage) return { ok: true };
  const key = composerDraftStorageKey(scope);
  try {
    storage.removeItem(key);
    return { ok: true };
  } catch (error) {
    return classifyWriteFailure(storage, key, error);
  }
}
