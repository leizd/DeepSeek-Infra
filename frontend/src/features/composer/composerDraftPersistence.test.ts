import { describe, expect, it } from "vitest";

import {
  clearComposerDraft,
  composerDraftStorageKey,
  loadComposerDraft,
  migrateLegacyDraft,
  saveComposerDraft,
  type ComposerDraft,
  type SessionStorageLike,
} from "./composerDraftPersistence";

class MemorySessionStorage implements SessionStorageLike {
  readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

/** 包装一个可用存储，但 setItem 一律抛出指定错误。 */
class FailingWriteStorage implements SessionStorageLike {
  constructor(
    private readonly inner: SessionStorageLike,
    private readonly error: unknown,
  ) {}

  getItem(key: string): string | null {
    return this.inner.getItem(key);
  }

  setItem(): void {
    throw this.error;
  }

  removeItem(key: string): void {
    this.inner.removeItem(key);
  }
}

/** 读写全部抛错：浏览器把存储整体禁用。 */
class DisabledStorage implements SessionStorageLike {
  getItem(): string | null {
    throw new Error("storage disabled");
  }

  setItem(): void {
    throw new Error("storage disabled");
  }

  removeItem(): void {
    throw new Error("storage disabled");
  }
}

/** 指定键的回读被篡改：写入看似成功，读回来的内容却不一致。 */
class MismatchReadbackStorage implements SessionStorageLike {
  private readonly values = new Map<string, string>();

  constructor(private readonly mismatchedKey: string) {}

  getItem(key: string): string | null {
    const value = this.values.get(key) ?? null;
    if (key === this.mismatchedKey && value !== null) {
      const parsed = JSON.parse(value) as { text: string };
      return JSON.stringify({ ...parsed, text: `${parsed.text} (tampered)` });
    }
    return value;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

const draftA: ComposerDraft = {
  conversationId: "conversation-a",
  projectId: null,
  text: "draft",
  updatedAt: 42,
};

describe("composer draft persistence", () => {
  it("scopes drafts by conversation and project, restoring each project independently", () => {
    const storage = new MemorySessionStorage();
    saveComposerDraft({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "draft a",
      updatedAt: 123,
    }, storage);
    saveComposerDraft({
      conversationId: "conversation-a",
      projectId: "project-b",
      text: "draft b",
      updatedAt: 456,
    }, storage);
    saveComposerDraft({
      conversationId: "conversation-a",
      projectId: null,
      text: "draft none",
      updatedAt: 789,
    }, storage);

    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: "project-a" }, storage)).toEqual({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "draft a",
      updatedAt: 123,
    });
    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: "project-b" }, storage)?.text).toBe("draft b");
    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: null }, storage)?.text).toBe("draft none");
    expect(loadComposerDraft({ conversationId: "conversation-b", projectId: "project-a" }, storage)).toBeNull();
  });

  it("encodes storage keys so special characters cannot collide across scopes", () => {
    const scoped = composerDraftStorageKey({ conversationId: "conv:x", projectId: "proj:y" });
    expect(scoped).toBe("deepseek:composer-draft:conv%3Ax:proj%3Ay");
    expect(composerDraftStorageKey({ conversationId: "conv", projectId: null })).toBe("deepseek:composer-draft:conv:");
    expect(scoped).not.toBe(composerDraftStorageKey({ conversationId: "conv", projectId: "x:proj:y" }));
  });

  it("migrates a legacy conversation-only draft exactly once into its recorded project", () => {
    const storage = new MemorySessionStorage();
    storage.setItem("deepseek:composer-draft:conversation-a", JSON.stringify({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "legacy draft",
      updatedAt: 1,
    }));

    // 另一个项目的作用域读取触发迁移，但不会把草稿绑定到自己。
    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: "project-b" }, storage)).toBeNull();
    expect(storage.getItem("deepseek:composer-draft:conversation-a")).toBeNull();
    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: "project-a" }, storage)).toEqual({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "legacy draft",
      updatedAt: 1,
    });
  });

  it("migrates a project-less legacy draft into the none scope, never the active project", () => {
    const storage = new MemorySessionStorage();
    storage.setItem("deepseek:composer-draft:conversation-a", JSON.stringify({
      conversationId: "conversation-a",
      text: "legacy none",
      updatedAt: 2,
    }));

    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: "project-b" }, storage)).toBeNull();
    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: null }, storage)?.text).toBe("legacy none");

    // 旧键已删除：再次保存新作用域草稿不会重复迁移。
    saveComposerDraft({
      conversationId: "conversation-a",
      projectId: "project-b",
      text: "new draft",
      updatedAt: 3,
    }, storage);
    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: null }, storage)?.text).toBe("legacy none");
    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: "project-b" }, storage)?.text).toBe("new draft");
  });

  it("clears empty or sent drafts without storing files or credentials", () => {
    const storage = new MemorySessionStorage();
    const scope = { conversationId: "conversation-a", projectId: null };
    const key = composerDraftStorageKey(scope);
    storage.setItem(key, JSON.stringify({
      conversationId: "conversation-a",
      text: "draft",
      updatedAt: 1,
      files: [{ name: "secret.txt" }],
      apiKey: "sk-secret",
    }));

    const restored = loadComposerDraft(scope, storage);
    expect(restored).toEqual({
      conversationId: "conversation-a",
      projectId: null,
      text: "draft",
      updatedAt: 1,
    });
    expect(restored).not.toHaveProperty("files");
    expect(restored).not.toHaveProperty("apiKey");

    clearComposerDraft(scope, storage);
    expect(storage.getItem(key)).toBeNull();
    saveComposerDraft({
      conversationId: "conversation-a",
      projectId: null,
      text: "",
      updatedAt: 2,
    }, storage);
    expect(storage.getItem(key)).toBeNull();
  });

  it("rejects corrupt, mismatched, and incomplete session values", () => {
    const storage = new MemorySessionStorage();
    const scope = { conversationId: "conversation-a", projectId: "project-a" };
    const key = composerDraftStorageKey(scope);
    storage.setItem(key, "{");
    expect(loadComposerDraft(scope, storage)).toBeNull();
    storage.setItem(key, JSON.stringify({
      conversationId: "conversation-b",
      projectId: "project-a",
      text: "wrong",
      updatedAt: 1,
    }));
    expect(loadComposerDraft(scope, storage)).toBeNull();
    storage.setItem(key, JSON.stringify({
      conversationId: "conversation-a",
      projectId: "project-b",
      text: "wrong project",
      updatedAt: 1,
    }));
    expect(loadComposerDraft(scope, storage)).toBeNull();
    storage.setItem(key, JSON.stringify({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: 42,
      updatedAt: 1,
    }));
    expect(loadComposerDraft(scope, storage)).toBeNull();
  });

  it("treats an empty draft as safe when session storage is unavailable", () => {
    expect(clearComposerDraft({ conversationId: "conversation-a", projectId: null }, null)).toEqual({ ok: true });
    expect(saveComposerDraft({
      conversationId: "conversation-a",
      projectId: null,
      text: "unsaved",
      updatedAt: 1,
    }, null)).toMatchObject({ ok: false, code: "storage-unavailable" });
  });
});

describe("composer draft failure codes", () => {
  it("returns the updatedAt revision on a successful save", () => {
    expect(saveComposerDraft(draftA, new MemorySessionStorage())).toEqual({ ok: true, revision: "42" });
  });

  it("maps QuotaExceededError to quota-exceeded, including legacy quota signals", () => {
    expect(saveComposerDraft(draftA, new FailingWriteStorage(
      new MemorySessionStorage(),
      new DOMException("full", "QuotaExceededError"),
    ))).toMatchObject({ ok: false, code: "quota-exceeded" });
    // Firefox 旧式错误名。
    expect(saveComposerDraft(draftA, new FailingWriteStorage(
      new MemorySessionStorage(),
      { name: "NS_ERROR_DOM_QUOTA_REACHED", message: "quota" },
    ))).toMatchObject({ ok: false, code: "quota-exceeded" });
    // 旧数值 code 22。
    expect(saveComposerDraft(draftA, new FailingWriteStorage(
      new MemorySessionStorage(),
      { name: "Error", code: 22 },
    ))).toMatchObject({ ok: false, code: "quota-exceeded" });
  });

  it("maps SecurityError to storage-unavailable", () => {
    expect(saveComposerDraft(draftA, new FailingWriteStorage(
      new MemorySessionStorage(),
      new DOMException("denied", "SecurityError"),
    ))).toMatchObject({ ok: false, code: "storage-unavailable" });
  });

  it("treats a write failure with unreadable storage as storage-unavailable", () => {
    expect(saveComposerDraft(draftA, new DisabledStorage()))
      .toMatchObject({ ok: false, code: "storage-unavailable" });
    expect(clearComposerDraft(
      { conversationId: "conversation-a", projectId: null },
      new DisabledStorage(),
    )).toMatchObject({ ok: false, code: "storage-unavailable" });
  });

  it("keeps unknown for unrecognized write failures when reads still work", () => {
    expect(saveComposerDraft(draftA, new FailingWriteStorage(new MemorySessionStorage(), new Error("boom"))))
      .toMatchObject({ ok: false, code: "unknown", message: "boom" });
  });

  it("reports verification-failed when the read-back does not match the written draft", () => {
    const scope = { conversationId: "conversation-a", projectId: null };
    const storage = new MismatchReadbackStorage(composerDraftStorageKey(scope));
    expect(saveComposerDraft({ ...scope, text: "draft", updatedAt: 9 }, storage))
      .toMatchObject({ ok: false, code: "verification-failed" });
  });
});

describe("lossless legacy migration", () => {
  const legacyKey = "deepseek:composer-draft:conversation-a";

  it("writes and verifies the scoped key before deleting the legacy key", () => {
    const storage = new MemorySessionStorage();
    storage.setItem(legacyKey, JSON.stringify({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "legacy draft",
      updatedAt: 7,
    }));

    expect(migrateLegacyDraft("conversation-a", storage)).toEqual({ ok: true, revision: "7" });
    expect(storage.getItem(legacyKey)).toBeNull();
    const scopedKey = composerDraftStorageKey({ conversationId: "conversation-a", projectId: "project-a" });
    expect(JSON.parse(storage.getItem(scopedKey) ?? "null")).toMatchObject({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "legacy draft",
    });
  });

  it("keeps the legacy key and still loads the draft when the migration write throws", () => {
    const inner = new MemorySessionStorage();
    inner.setItem(legacyKey, JSON.stringify({
      conversationId: "conversation-a",
      projectId: null,
      text: "keep me",
      updatedAt: 3,
    }));
    const storage = new FailingWriteStorage(inner, new DOMException("full", "QuotaExceededError"));

    expect(migrateLegacyDraft("conversation-a", storage)).toMatchObject({ ok: false, code: "quota-exceeded" });
    // 旧键必须原样保留。
    expect(storage.getItem(legacyKey)).not.toBeNull();
    // 草稿仍可从旧键加载。
    expect(loadComposerDraft({ conversationId: "conversation-a", projectId: null }, storage)?.text).toBe("keep me");
    expect(storage.getItem(legacyKey)).not.toBeNull();
  });

  it("keeps the legacy key when the read-back does not match the migrated draft", () => {
    const scopedKey = composerDraftStorageKey({ conversationId: "conversation-a", projectId: "project-a" });
    const storage = new MismatchReadbackStorage(scopedKey);
    storage.setItem(legacyKey, JSON.stringify({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "legacy",
      updatedAt: 5,
    }));

    expect(migrateLegacyDraft("conversation-a", storage)).toMatchObject({ ok: false, code: "verification-failed" });
    expect(storage.getItem(legacyKey)).not.toBeNull();
  });

  it("cleans up unparseable legacy content exactly once without a draft to lose", () => {
    const storage = new MemorySessionStorage();
    storage.setItem(legacyKey, "{");

    expect(migrateLegacyDraft("conversation-a", storage)).toEqual({ ok: true });
    expect(storage.getItem(legacyKey)).toBeNull();
    expect(migrateLegacyDraft("conversation-a", storage)).toBeNull();
  });
});
