export interface ComposerDraft {
  conversationId: string;
  projectId?: string;
  text: string;
  updatedAt: number;
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

export function composerDraftStorageKey(conversationId: string): string {
  return `${KEY_PREFIX}${conversationId}`;
}

export function loadComposerDraft(
  conversationId: string,
  storage: SessionStorageLike | null = browserSessionStorage(),
): ComposerDraft | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(composerDraftStorageKey(conversationId));
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<ComposerDraft>;
    if (
      value.conversationId !== conversationId
      || typeof value.text !== "string"
      || typeof value.updatedAt !== "number"
      || (value.projectId !== undefined && typeof value.projectId !== "string")
    ) {
      return null;
    }
    return {
      conversationId,
      projectId: value.projectId,
      text: value.text,
      updatedAt: value.updatedAt,
    };
  } catch {
    return null;
  }
}

export function saveComposerDraft(
  draft: ComposerDraft,
  storage: SessionStorageLike | null = browserSessionStorage(),
): boolean {
  if (!storage) return false;
  try {
    if (!draft.text) {
      storage.removeItem(composerDraftStorageKey(draft.conversationId));
      return true;
    }
    storage.setItem(composerDraftStorageKey(draft.conversationId), JSON.stringify(draft));
    return true;
  } catch {
    return false;
  }
}

export function clearComposerDraft(
  conversationId: string,
  storage: SessionStorageLike | null = browserSessionStorage(),
): boolean {
  if (!storage) return true;
  try {
    storage.removeItem(composerDraftStorageKey(conversationId));
    return true;
  } catch {
    return false;
  }
}
