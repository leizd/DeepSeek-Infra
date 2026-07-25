import { describe, expect, it } from "vitest";

import type { Conversation } from "./types";
import {
  conversationStorageKeys,
  estimateCheckpointBytes,
  loadPersistedConversationState,
  savePersistedConversationState,
  sessionSnapshotKey,
  type StorageLike,
} from "./persistence";

class MemoryStorage implements StorageLike {
  readonly values = new Map<string, string>();
  failOnSet: ((key: string) => boolean) | null = null;
  corruptOnSet: ((key: string) => boolean) | null = null;
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) {
    if (this.failOnSet?.(key)) throw new Error("setItem failed");
    this.values.set(key, this.corruptOnSet?.(key) ? `${value}#corrupted` : value);
  }
  removeItem(key: string) { this.values.delete(key); }
}

function makeConversation(id: string, content: string): Conversation {
  return {
    id,
    title: `会话 ${id}`,
    messages: [{
      id: `${id}-message`,
      role: "user",
      content,
      reasoning: "",
      createdAt: 100,
      phase: "done",
      streaming: false,
      attachments: [],
      timeline: [],
      systemNotes: [],
    }],
    model: "deepseek-v4-pro",
    thinkingEnabled: false,
    createdAt: 100,
    updatedAt: 200,
  };
}

function makeState(currentConversationId: string | null, conversations: Conversation[]) {
  return { schemaVersion: 1 as const, currentConversationId, conversations };
}

const LEGACY_CONVERSATION = {
  id: "legacy-1",
  title: "Legacy",
  messages: [{ id: "legacy-message-1", role: "user", content: "旧消息", createdAt: 100 }],
  createdAt: 100,
  updatedAt: 200,
};

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

  it("restores agent cards with content and the run cursor", () => {
    const storage = new MemoryStorage();
    storage.setItem(conversationStorageKeys.conversations, JSON.stringify([{
      id: "agent-1",
      title: "Agent",
      model: "deepseek-v4-pro",
      messages: [{
        id: "assistant-9",
        role: "assistant",
        content: "answer",
        agentRunId: "run_x",
        agentRunStatus: "running",
        agentRunLastEventIndex: 41,
        agentPlan: [{ id: "coder", task: "写代码" }],
        agentPlanLabel: "Leader 自动拆解",
        timeline: [
          { type: "agent", id: "agent-coder", phase: "coder", status: "running", name: "Coder", output: "片段", notes: ["n1"], durationMs: 5 },
          { type: "search", id: "s-coder-main", phase: "coder", status: "searching", search: { query: "q" } },
        ],
        createdAt: 100,
      }],
      createdAt: 100,
      updatedAt: 200,
    }]));
    const state = loadPersistedConversationState(storage);
    const message = state.conversations[0]?.messages[0];
    expect(message).toMatchObject({ agentRunId: "run_x", agentRunStatus: "running", agentRunLastEventIndex: 41, agentPlanLabel: "Leader 自动拆解" });
    expect(message?.agentPlan?.[0]).toMatchObject({ id: "coder", task: "写代码" });
    expect(message?.timeline[0]).toMatchObject({ id: "agent-coder", name: "Coder", output: "片段", status: "error" });
    expect(message?.timeline[1]).toMatchObject({ status: "error" });
  });

  it("stores conversations but never creates credential keys", () => {
    const storage = new MemoryStorage();
    const state = loadPersistedConversationState(storage);
    savePersistedConversationState(state, storage);
    expect(storage.values.has(sessionSnapshotKey(1))).toBe(true);
    expect([...storage.values.keys()].some((key) => /api-key|tavily-key/i.test(key))).toBe(false);
  });
});

describe("conversation persistence journal", () => {
  it("round-trips the committed state through the V2 checkpoint", () => {
    const storage = new MemoryStorage();
    const state = makeState("alpha", [makeConversation("alpha", "持久化内容")]);

    expect(savePersistedConversationState(state, storage)).toEqual({ ok: true, revision: "1" });

    const loaded = loadPersistedConversationState(storage);
    expect(loaded.currentConversationId).toBe("alpha");
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["alpha"]);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("持久化内容");
  });

  it("returns monotonic revisions across commits", () => {
    const storage = new MemoryStorage();
    expect(savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "一")]), storage))
      .toEqual({ ok: true, revision: "1" });
    expect(savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "二")]), storage))
      .toEqual({ ok: true, revision: "2" });
  });

  it("fails without touching head or legacy keys when the snapshot write throws", () => {
    const storage = new MemoryStorage();
    storage.setItem(conversationStorageKeys.conversations, JSON.stringify([LEGACY_CONVERSATION]));
    storage.setItem(conversationStorageKeys.currentConversation, "legacy-1");
    const state = makeState("alpha", [makeConversation("alpha", "新状态")]);
    storage.failOnSet = (key) => key === sessionSnapshotKey(1);

    const result = savePersistedConversationState(state, storage);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.message).toBe(`setItem failed (~${estimateCheckpointBytes(state)} bytes)`);
    expect(storage.getItem(conversationStorageKeys.sessionHead)).toBeNull();
    expect(storage.getItem(conversationStorageKeys.conversations)).not.toBeNull();
    expect(storage.getItem(conversationStorageKeys.currentConversation)).toBe("legacy-1");
    const loaded = loadPersistedConversationState(storage);
    expect(loaded.currentConversationId).toBe("legacy-1");
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["legacy-1"]);
  });

  it("reports verification-failed when the snapshot read-back differs from what was written", () => {
    const storage = new MemoryStorage();
    storage.corruptOnSet = (key) => key.startsWith("deepseek-infra.session.v2.snapshot.");

    const result = savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "内容")]), storage);

    expect(result).toMatchObject({ ok: false, code: "verification-failed" });
    if (result.ok) return;
    expect(result.message).toContain(`(~${estimateCheckpointBytes(makeState("alpha", [makeConversation("alpha", "内容")]))} bytes)`);
    expect(storage.getItem(conversationStorageKeys.sessionHead)).toBeNull();
  });

  it("keeps loading the previous generation when the head write throws", () => {
    const storage = new MemoryStorage();
    const first = makeState("alpha", [makeConversation("alpha", "第一代")]);
    expect(savePersistedConversationState(first, storage)).toEqual({ ok: true, revision: "1" });

    storage.failOnSet = (key) => key === conversationStorageKeys.sessionHead;
    const second = makeState("beta", [makeConversation("beta", "第二代")]);
    const result = savePersistedConversationState(second, storage);

    expect(result.ok).toBe(false);
    expect(storage.getItem(conversationStorageKeys.sessionHead)).toBe("1");
    const loaded = loadPersistedConversationState(storage);
    // currentConversationId 与 conversations 必须来自同一份旧 generation，不得混合。
    expect(loaded.currentConversationId).toBe("alpha");
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["alpha"]);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("第一代");
  });

  it("falls back to generation N-1 when the head snapshot is corrupt", () => {
    const storage = new MemoryStorage();
    savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "第一代")]), storage);
    savePersistedConversationState(makeState("beta", [makeConversation("beta", "第二代")]), storage);
    storage.setItem(sessionSnapshotKey(2), "garbage{{{");

    const loaded = loadPersistedConversationState(storage);

    expect(loaded.currentConversationId).toBe("alpha");
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["alpha"]);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("第一代");
  });

  it("deletes legacy keys only after the first verified V2 commit", () => {
    const storage = new MemoryStorage();
    storage.setItem(conversationStorageKeys.conversations, JSON.stringify([LEGACY_CONVERSATION]));
    storage.setItem(conversationStorageKeys.currentConversation, "legacy-1");
    storage.setItem(conversationStorageKeys.legacyMessages, JSON.stringify([{ id: "lm-1", role: "user", content: "更旧", createdAt: 50 }]));

    // 首次提交失败：legacy 键必须原样保留。
    storage.failOnSet = (key) => key === sessionSnapshotKey(1);
    const failed = savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "新")]), storage);
    expect(failed.ok).toBe(false);
    expect(storage.values.has(conversationStorageKeys.conversations)).toBe(true);
    expect(storage.getItem(conversationStorageKeys.currentConversation)).toBe("legacy-1");

    // 首次提交成功：legacy conversations / currentConversation 键被移除，
    // v1 读取器此刻已找不到任何 legacy 会话数据；legacyMessages 永不删除。
    storage.failOnSet = null;
    const committed = savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "新")]), storage);
    expect(committed).toEqual({ ok: true, revision: "1" });
    expect(storage.values.has(conversationStorageKeys.conversations)).toBe(false);
    expect(storage.values.has(conversationStorageKeys.currentConversation)).toBe(false);
    expect(storage.values.has(conversationStorageKeys.legacyMessages)).toBe(true);
  });

  it("retains only the latest two snapshots", () => {
    const storage = new MemoryStorage();
    savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "一")]), storage);
    savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "二")]), storage);
    savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "三")]), storage);

    expect(storage.values.has(sessionSnapshotKey(1))).toBe(false);
    expect(storage.values.has(sessionSnapshotKey(2))).toBe(true);
    expect(storage.values.has(sessionSnapshotKey(3))).toBe(true);
    expect(storage.getItem(conversationStorageKeys.sessionHead)).toBe("3");
  });
});
