import { describe, expect, it } from "vitest";

import { conversationStorageKeys, loadPersistedConversationState, savePersistedConversationState, type StorageLike } from "./persistence";

class MemoryStorage implements StorageLike {
  readonly values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

describe("conversation persistence", () => {
  it("migrates legacy messages without dropping activity, diagnostics, preview, or interruption", () => {
    const storage = new MemoryStorage();
    storage.setItem(conversationStorageKeys.conversations, JSON.stringify([{
      id: "legacy-1",
      title: "Legacy",
      model: "deepseek-reasoner",
      messages: [{
        id: "assistant-1",
        role: "assistant",
        content: "partial",
        reasoning: "thought",
        interrupted: true,
        timeline: [{ kind: "reasoning", text: "step" }],
        search: { query: "docs" },
        diagnostics: { traceId: "trace-1" },
        attachments: [{ name: "image.png", preview: "data:image/png;base64,preview" }],
        createdAt: 100,
      }],
      createdAt: 100,
      updatedAt: 200,
    }]));
    storage.setItem(conversationStorageKeys.currentConversation, "legacy-1");

    const state = loadPersistedConversationState(storage);
    const message = state.conversations[0]?.messages[0];
    expect(state.currentConversationId).toBe("legacy-1");
    expect(state.conversations[0]?.model).toBe("deepseek-v4-pro");
    expect(message).toMatchObject({ phase: "interrupted", interrupted: true, search: { query: "docs" }, diagnostics: { traceId: "trace-1" } });
    expect(message?.timeline[0]).toMatchObject({ type: "reasoning", text: "step" });
    expect(message?.attachments[0]?.preview).toContain("data:image/png");
  });

  it("stores conversations but never creates credential keys", () => {
    const storage = new MemoryStorage();
    const state = loadPersistedConversationState(storage);
    savePersistedConversationState(state, storage);
    expect(storage.values.has(conversationStorageKeys.conversations)).toBe(true);
    expect([...storage.values.keys()].some((key) => /api-key|tavily-key/i.test(key))).toBe(false);
  });
});
