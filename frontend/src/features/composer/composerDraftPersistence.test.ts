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
  it("isolates drafts by conversation and restores project metadata", () => {
    const storage = new MemorySessionStorage();
    saveComposerDraft({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "draft a",
      updatedAt: 123,
    }, storage);
    saveComposerDraft({
      conversationId: "conversation-b",
      text: "draft b",
      updatedAt: 456,
    }, storage);

    expect(loadComposerDraft("conversation-a", storage)).toEqual({
      conversationId: "conversation-a",
      projectId: "project-a",
      text: "draft a",
      updatedAt: 123,
    });
    expect(loadComposerDraft("conversation-b", storage)?.text).toBe("draft b");
  });

  it("clears empty or sent drafts without storing files or credentials", () => {
    const storage = new MemorySessionStorage();
    const key = composerDraftStorageKey("conversation-a");
    storage.setItem(key, JSON.stringify({
      conversationId: "conversation-a",
      text: "draft",
      updatedAt: 1,
      files: [{ name: "secret.txt" }],
      apiKey: "sk-secret",
    }));

    const restored = loadComposerDraft("conversation-a", storage);
    expect(restored).toEqual({
      conversationId: "conversation-a",
      text: "draft",
      updatedAt: 1,
    });
    expect(restored).not.toHaveProperty("files");
    expect(restored).not.toHaveProperty("apiKey");

    clearComposerDraft("conversation-a", storage);
    expect(storage.getItem(key)).toBeNull();
    saveComposerDraft({
      conversationId: "conversation-a",
      text: "",
      updatedAt: 2,
    }, storage);
    expect(storage.getItem(key)).toBeNull();
  });

  it("rejects corrupt, mismatched, and incomplete session values", () => {
    const storage = new MemorySessionStorage();
    const key = composerDraftStorageKey("conversation-a");
    storage.setItem(key, "{");
    expect(loadComposerDraft("conversation-a", storage)).toBeNull();
    storage.setItem(key, JSON.stringify({
      conversationId: "conversation-b",
      text: "wrong",
      updatedAt: 1,
    }));
    expect(loadComposerDraft("conversation-a", storage)).toBeNull();
    storage.setItem(key, JSON.stringify({
      conversationId: "conversation-a",
      text: 42,
      updatedAt: 1,
    }));
    expect(loadComposerDraft("conversation-a", storage)).toBeNull();
  });

  it("treats an empty draft as safe when session storage is unavailable", () => {
    expect(clearComposerDraft("conversation-a", null)).toBe(true);
    expect(saveComposerDraft({
      conversationId: "conversation-a",
      text: "unsaved",
      updatedAt: 1,
    }, null)).toBe(false);
  });
});
