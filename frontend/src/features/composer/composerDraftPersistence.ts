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

/**
 * 旧版草稿只按会话保存。第一次读取时把它迁移到会话 + 项目的作用域键：
 * 草稿里带有项目 ID 就归入该项目，无法确定时归入无项目（none），
 * 绝不自动绑定当前项目。无论内容是否有效，旧键只迁移一次。
 */
function migrateLegacyDraft(conversationId: string, storage: SessionStorageLike): void {
  const legacyKey = legacyDraftStorageKey(conversationId);
  let raw: string | null;
  try {
    raw = storage.getItem(legacyKey);
  } catch {
    return;
  }
  if (raw === null) return;
  try {
    storage.removeItem(legacyKey);
  } catch {
    return;
  }
  const draft = parseDraft(raw, conversationId);
  if (!draft || !draft.text) return;
  saveComposerDraft(draft, storage);
}

export function loadComposerDraft(
  scope: ComposerDraftScope,
  storage: SessionStorageLike | null = browserSessionStorage(),
): ComposerDraft | null {
  if (!storage) return null;
  migrateLegacyDraft(scope.conversationId, storage);
  try {
    const raw = storage.getItem(composerDraftStorageKey(scope));
    if (!raw) return null;
    const draft = parseDraft(raw, scope.conversationId);
    if (!draft || (draft.projectId ?? null) !== scope.projectId) return null;
    return { ...draft, projectId: scope.projectId };
  } catch {
    return null;
  }
}

export function saveComposerDraft(
  draft: ComposerDraft,
  storage: SessionStorageLike | null = browserSessionStorage(),
): boolean {
  if (!storage) return false;
  const key = composerDraftStorageKey({
    conversationId: draft.conversationId,
    projectId: draft.projectId ?? null,
  });
  try {
    if (!draft.text) {
      storage.removeItem(key);
      return true;
    }
    storage.setItem(key, JSON.stringify(draft));
    return true;
  } catch {
    return false;
  }
}

export function clearComposerDraft(
  scope: ComposerDraftScope,
  storage: SessionStorageLike | null = browserSessionStorage(),
): boolean {
  if (!storage) return true;
  try {
    storage.removeItem(composerDraftStorageKey(scope));
    return true;
  } catch {
    return false;
  }
}
