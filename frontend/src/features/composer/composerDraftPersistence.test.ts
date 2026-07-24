import { describe, expect, it } from "vitest";

import {
  clearComposerDraft,
  composerDraftStorageKey,
  loadComposerDraft,
  saveComposerDraft,
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
    expect(clearComposerDraft({ conversationId: "conversation-a", projectId: null }, null)).toBe(true);
    expect(saveComposerDraft({
      conversationId: "conversation-a",
      projectId: null,
      text: "unsaved",
      updatedAt: 1,
    }, null)).toBe(false);
  });
});
