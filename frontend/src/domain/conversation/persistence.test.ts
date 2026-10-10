import { beforeEach, describe, expect, it, vi } from "vitest";

import { getTabId, TAB_ID_STORAGE_KEY } from "../../app/tabIdentity";
import type { ChatMessage } from "../chat/types";
import type { Conversation } from "./types";
import { checkpointMessage, INTERRUPTED_CHECKPOINT_NOTE } from "./checkpoint";
import {
  compactConversationForStorage,
  PREVIEW_COMPACTION_MIN_BYTES,
  TIMELINE_RAW_PAYLOAD_CAP_BYTES,
} from "./compaction";
import {
  checkpointDigest,
  conversationStorageKeys,
  createConversationPersistenceAdapter as createBasePersistenceAdapter,
  estimateConversationBytes,
  loadPersistedConversationState,
  resetConversationPersistenceForTests,
  runIdleCheckpointGc,
  savePersistedConversationState,
  sessionHeadKeyV3,
  sessionSnapshotKey,
  sessionSnapshotKeyV3,
  sessionTombstoneKeyV3,
  type ConversationCheckpointV2,
  type ConversationCheckpointV3,
  type ConversationCompactionSignal,
  type StorageLike,
  type ConversationPersistenceAdapterOptions,
} from "./persistence";

const TEST_TAB_ID = "deadbeef";

function createConversationPersistenceAdapter(options: ConversationPersistenceAdapterOptions = {}) {
  return createBasePersistenceAdapter({
    ...options,
    identity: options.identity ?? { writerSessionId: TEST_TAB_ID, documentInstanceId: "document-deadbeef" },
  });
}

class MemoryStorage implements StorageLike {
  readonly values = new Map<string, string>();
  failOnSet: ((key: string) => boolean) | null = null;
  corruptOnSet: ((key: string) => boolean) | null = null;
  quotaOnSet: ((key: string, value: string) => boolean) | null = null;
  enumerationEnabled = true;
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) {
    if (this.quotaOnSet?.(key, value)) throw Object.assign(new Error("setItem failed: quota"), { name: "QuotaExceededError" });
    if (this.failOnSet?.(key)) throw new Error("setItem failed");
    this.values.set(key, this.corruptOnSet?.(key) ? `${value}#corrupted` : value);
  }
  removeItem(key: string) { this.values.delete(key); }
  get length(): number | undefined { return this.enumerationEnabled ? this.values.size : undefined; }
  key(index: number): string | null {
    if (!this.enumerationEnabled) return null;
    return [...this.values.keys()][index] ?? null;
  }
}

function makeSession(tabId: string | null = TEST_TAB_ID): MemoryStorage {
  const session = new MemoryStorage();
  if (tabId) session.setItem(TAB_ID_STORAGE_KEY, tabId);
  return session;
}

function makeMessage(id: string, content: string): ChatMessage {
  return {
    id,
    role: "user",
    content,
    reasoning: "",
    createdAt: 100,
    phase: "done",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
  };
}

function makeConversation(id: string, content: string): Conversation {
  return {
    id,
    title: `会话 ${id}`,
    messages: [makeMessage(`${id}-message`, content)],
    model: "deepseek-v4-pro",
    thinkingEnabled: false,
    createdAt: 100,
    updatedAt: 200,
  };
}

function makeState(currentConversationId: string | null, conversations: Conversation[]) {
  return { schemaVersion: 1 as const, currentConversationId, conversations };
}

function seedV2Journal(
  storage: MemoryStorage,
  entries: { generation: number; conversations: unknown[]; currentConversationId?: string | null }[],
): void {
  for (const entry of entries) {
    const checkpoint: ConversationCheckpointV2 = {
      schemaVersion: 2,
      generation: entry.generation,
      savedAt: 1000 + entry.generation,
      currentConversationId: entry.currentConversationId ?? null,
      conversations: entry.conversations as Conversation[],
    };
    storage.setItem(sessionSnapshotKey(entry.generation), JSON.stringify(checkpoint));
  }
  storage.setItem(conversationStorageKeys.sessionHead, String(entries.at(-1)?.generation ?? 0));
}

function v3SnapshotKeys(storage: MemoryStorage, conversationId?: string): string[] {
  const prefix = conversationId ? sessionSnapshotKeyV3(conversationId, "") : conversationStorageKeys.v3SnapshotPrefix;
  return [...storage.values.keys()].filter((key) => key.startsWith(prefix));
}

const LEGACY_CONVERSATION = {
  id: "legacy-1",
  title: "Legacy",
  messages: [{ id: "legacy-message-1", role: "user", content: "旧消息", createdAt: 100 }],
  createdAt: 100,
  updatedAt: 200,
};

beforeEach(() => {
  resetConversationPersistenceForTests();
});

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
    savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "内容")]), storage, makeSession());
    expect(v3SnapshotKeys(storage).length).toBeGreaterThan(0);
    expect([...storage.values.keys()].some((key) => /api-key|tavily-key/i.test(key))).toBe(false);
  });
});

describe("V2 journal reader (migration path)", () => {
  it("loads the head generation from a seeded V2 journal", () => {
    const storage = new MemoryStorage();
    seedV2Journal(storage, [
      { generation: 1, conversations: [LEGACY_CONVERSATION], currentConversationId: "legacy-1" },
    ]);

    const loaded = loadPersistedConversationState(storage);

    expect(loaded.currentConversationId).toBe("legacy-1");
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["legacy-1"]);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("旧消息");
  });

  it("falls back to generation N-1 when the head snapshot is corrupt", () => {
    const storage = new MemoryStorage();
    seedV2Journal(storage, [
      { generation: 1, conversations: [{ ...LEGACY_CONVERSATION, id: "legacy-1" }], currentConversationId: "legacy-1" },
      { generation: 2, conversations: [{ ...LEGACY_CONVERSATION, id: "legacy-2" }], currentConversationId: "legacy-2" },
    ]);
    storage.setItem(sessionSnapshotKey(2), "garbage{{{");

    const loaded = loadPersistedConversationState(storage);

    expect(loaded.currentConversationId).toBe("legacy-1");
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["legacy-1"]);
  });

  it("falls back to legacy keys when the journal is unusable", () => {
    const storage = new MemoryStorage();
    storage.setItem(conversationStorageKeys.sessionHead, "3");
    storage.setItem(conversationStorageKeys.conversations, JSON.stringify([LEGACY_CONVERSATION]));
    storage.setItem(conversationStorageKeys.currentConversation, "legacy-1");

    const loaded = loadPersistedConversationState(storage);

    expect(loaded.currentConversationId).toBe("legacy-1");
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["legacy-1"]);
  });
});

describe("V3 sharded commits", () => {
  it("round-trips two conversations through per-conversation shards", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const state = makeState("alpha", [makeConversation("alpha", "一"), makeConversation("beta", "二")]);

    expect(savePersistedConversationState(state, storage, session)).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });

    const loaded = loadPersistedConversationState(storage, session);
    expect(loaded.currentConversationId).toBe("alpha");
    expect(new Set(loaded.conversations.map((conversation) => conversation.id))).toEqual(new Set(["alpha", "beta"]));
    const contents = Object.fromEntries(loaded.conversations.map((conversation) => [conversation.id, conversation.messages[0]?.content]));
    expect(contents).toEqual({ alpha: "一", beta: "二" });
  });

  it("returns monotonic per-conversation revisions across commits", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    let conversation = makeConversation("alpha", "一");
    expect(savePersistedConversationState(makeState("alpha", [conversation]), storage, session))
      .toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });
    conversation = { ...conversation, updatedAt: 300 };
    expect(savePersistedConversationState(makeState("alpha", [conversation]), storage, session))
      .toEqual({ ok: true, revision: `2.${TEST_TAB_ID}` });
    const head = JSON.parse(storage.getItem(sessionHeadKeyV3("alpha")) ?? "{}") as Record<string, unknown>;
    expect(head.revision).toBe(`2.${TEST_TAB_ID}`);
    expect(head.parentRevision).toBe(`1.${TEST_TAB_ID}`);
    expect(head.writerId).toBe(TEST_TAB_ID);
  });

  it("serializes only the dirty conversation when one of two changes", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const alpha = makeConversation("alpha", "一");
    const beta = makeConversation("beta", "二");
    savePersistedConversationState(makeState("alpha", [alpha, beta]), storage, session);

    const setSpy = vi.spyOn(storage, "setItem");
    const changedBeta: Conversation = { ...beta, messages: [...beta.messages, makeMessage("beta-2", "二·改")], updatedAt: 300 };
    const result = savePersistedConversationState(makeState("alpha", [alpha, changedBeta]), storage, session);

    expect(result).toEqual({ ok: true, revision: `2.${TEST_TAB_ID}` });
    const snapshotWrites = setSpy.mock.calls.filter(([key]) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix));
    expect(snapshotWrites).toHaveLength(1);
    expect(snapshotWrites[0]?.[0]).toBe(sessionSnapshotKeyV3("beta", `2.${TEST_TAB_ID}`));
    const checkpoint = JSON.parse(snapshotWrites[0]?.[1] ?? "{}") as ConversationCheckpointV3;
    expect(checkpoint.schemaVersion).toBe(3);
    expect(checkpoint.conversationId).toBe("beta");
    expect(checkpoint.parentRevision).toBe(`1.${TEST_TAB_ID}`);
    expect(checkpoint.digest).toBe(checkpointDigest(JSON.stringify(checkpoint.conversation)));
    expect(checkpoint.conversation.messages.at(-1)?.content).toBe("二·改");
    // 未变更的 alpha 既没有重写快照，也没有重写 head。
    expect(setSpy.mock.calls.some(([key]) => key.includes("alpha"))).toBe(false);
  });

  it("skips storage writes entirely when nothing is dirty", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const state = makeState("alpha", [makeConversation("alpha", "一")]);
    savePersistedConversationState(state, storage, session);

    const setSpy = vi.spyOn(storage, "setItem");
    const result = savePersistedConversationState(state, storage, session);

    expect(result).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });
    expect(setSpy).not.toHaveBeenCalled();
  });

  it("fails without touching head or legacy keys when the snapshot write throws", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    storage.setItem(conversationStorageKeys.conversations, JSON.stringify([LEGACY_CONVERSATION]));
    storage.setItem(conversationStorageKeys.currentConversation, "legacy-1");
    const conversation = makeConversation("alpha", "新状态");
    const state = makeState("alpha", [conversation]);
    storage.failOnSet = (key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix);

    const result = savePersistedConversationState(state, storage, session);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.message).toBe(`会话 alpha：setItem failed (~${estimateConversationBytes(conversation)} bytes)`);
    expect(storage.getItem(sessionHeadKeyV3("alpha"))).toBeNull();
    expect(storage.getItem(conversationStorageKeys.conversations)).not.toBeNull();
    expect(storage.getItem(conversationStorageKeys.currentConversation)).toBe("legacy-1");
    const loaded = loadPersistedConversationState(storage, session);
    expect(loaded.currentConversationId).toBe("legacy-1");
    expect(loaded.conversations.map((item) => item.id)).toEqual(["legacy-1"]);
  });

  it("reports verification-failed when the snapshot read-back differs from what was written", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const conversation = makeConversation("alpha", "内容");
    storage.corruptOnSet = (key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix);

    const result = savePersistedConversationState(makeState("alpha", [conversation]), storage, session);

    expect(result).toMatchObject({ ok: false, code: "verification-failed" });
    if (result.ok) return;
    expect(result.message).toContain("会话 alpha");
    expect(result.message).toContain(`(~${estimateConversationBytes(conversation)} bytes)`);
    expect(storage.getItem(sessionHeadKeyV3("alpha"))).toBeNull();
  });

  it("keeps the previous head loadable when the head write throws, tolerating the orphan snapshot", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const first = makeConversation("alpha", "第一版");
    expect(savePersistedConversationState(makeState("alpha", [first]), storage, session))
      .toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });

    storage.failOnSet = (key) => key === sessionHeadKeyV3("alpha");
    const second: Conversation = { ...first, messages: [makeMessage("alpha-2", "第二版")], updatedAt: 300 };
    const result = savePersistedConversationState(makeState("alpha", [second]), storage, session);

    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.message).toContain("会话 alpha");
    // 孤儿快照残留但不被引用；head 仍指向第一版，加载结果不受撕裂写影响。
    expect(v3SnapshotKeys(storage, "alpha")).toContain(sessionSnapshotKeyV3("alpha", `2.${TEST_TAB_ID}`));
    const head = JSON.parse(storage.getItem(sessionHeadKeyV3("alpha")) ?? "{}") as Record<string, unknown>;
    expect(head.revision).toBe(`1.${TEST_TAB_ID}`);
    const loaded = loadPersistedConversationState(storage, session);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("第一版");
  });

  it("falls back to the parentRevision snapshot when the head snapshot is corrupt", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const first = makeConversation("alpha", "第一代");
    savePersistedConversationState(makeState("alpha", [first]), storage, session);
    const second: Conversation = { ...first, messages: [makeMessage("alpha-2", "第二代")], updatedAt: 300 };
    savePersistedConversationState(makeState("alpha", [second]), storage, session);
    storage.setItem(sessionSnapshotKeyV3("alpha", `2.${TEST_TAB_ID}`), "garbage{{{");

    const loaded = loadPersistedConversationState(storage, session);

    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["alpha"]);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("第一代");
  });

  it("rejects a snapshot whose digest does not match its payload", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const conversation = makeConversation("alpha", "原始");
    savePersistedConversationState(makeState("alpha", [conversation]), storage, session);
    const raw = storage.getItem(sessionSnapshotKeyV3("alpha", `1.${TEST_TAB_ID}`));
    const tampered = JSON.parse(raw ?? "{}") as ConversationCheckpointV3;
    tampered.conversation = { ...tampered.conversation, title: "被篡改" };
    storage.setItem(sessionSnapshotKeyV3("alpha", `1.${TEST_TAB_ID}`), JSON.stringify(tampered));

    const loaded = loadPersistedConversationState(storage, session);

    expect(loaded.conversations).toEqual([]);
  });

  it("retains at most two snapshots per conversation with bounded removals", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    let conversation = makeConversation("alpha", "一");
    const revisions: string[] = [];
    for (const content of ["一", "二", "三"]) {
      conversation = { ...conversation, messages: [makeMessage(`alpha-${content}`, content)], updatedAt: conversation.updatedAt + 1 };
      const result = savePersistedConversationState(makeState("alpha", [conversation]), storage, session);
      expect(result.ok).toBe(true);
      if (result.ok && result.revision) revisions.push(result.revision);
    }

    expect(revisions).toEqual([`1.${TEST_TAB_ID}`, `2.${TEST_TAB_ID}`, `3.${TEST_TAB_ID}`]);
    expect(v3SnapshotKeys(storage, "alpha").sort()).toEqual([
      sessionSnapshotKeyV3("alpha", `2.${TEST_TAB_ID}`),
      sessionSnapshotKeyV3("alpha", `3.${TEST_TAB_ID}`),
    ]);
  });

  it("keeps per-commit storage cost constant across ten thousand commits", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    let conversation = makeConversation("alpha", "0");
    savePersistedConversationState(makeState("alpha", [conversation]), storage, session);
    const setSpy = vi.spyOn(storage, "setItem");
    const removeSpy = vi.spyOn(storage, "removeItem");

    for (let index = 1; index <= 10_000; index += 1) {
      conversation = { ...conversation, updatedAt: 200 + index };
      const result = savePersistedConversationState(makeState("alpha", [conversation]), storage, session);
      expect(result.ok).toBe(true);
    }

    // 每次提交恒为 2 次分片写入（snapshot + head；租约触碰不计入分片成本）与至多 2 次 removeItem（保留 GC）。
    const shardWrites = setSpy.mock.calls.filter(([key]) => !key.startsWith(conversationStorageKeys.v3TabPrefix));
    expect(shardWrites.length).toBe(20_000);
    expect(removeSpy.mock.calls.length).toBeLessThanOrEqual(20_000);
    expect(v3SnapshotKeys(storage, "alpha").length).toBeLessThanOrEqual(2);
    setSpy.mockRestore();
    removeSpy.mockRestore();
  });

  it("commits a tombstone before deleting the shard of a conversation", async () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const adapter = createConversationPersistenceAdapter();
    const alpha = makeConversation("alpha", "一");
    const beta = makeConversation("beta", "二");
    adapter.save(makeState("alpha", [alpha, beta]), storage, session);
    expect(storage.getItem(sessionHeadKeyV3("beta"))).not.toBeNull();

    const result = await adapter.deleteConversationArbitrated("beta", storage, session);

    expect(result.ok).toBe(true);
    // 删除先落 tombstone（含被删 head 的 parentRevision），再删 head 与快照。
    const tombstone = JSON.parse(storage.getItem(sessionTombstoneKeyV3("beta")) ?? "null") as Record<string, unknown> | null;
    expect(tombstone).toMatchObject({
      conversationId: "beta",
      deletedRevision: `2.${TEST_TAB_ID}`,
      parentRevision: `1.${TEST_TAB_ID}`,
      writerId: TEST_TAB_ID,
    });
    expect(storage.getItem(sessionHeadKeyV3("beta"))).toBeNull();
    expect(v3SnapshotKeys(storage, "beta")).toEqual([]);
    const loaded = loadPersistedConversationState(storage, session);
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["alpha"]);
  });

  it("skips tombstoned conversations when loading V3 shards", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    savePersistedConversationState(
      makeState("alpha", [makeConversation("alpha", "一"), makeConversation("beta", "二")]),
      storage,
      session,
    );
    storage.setItem(sessionTombstoneKeyV3("beta"), "1");

    const loaded = loadPersistedConversationState(storage, session);

    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["alpha"]);
  });

  it("commits without enumeration support and skips GC silently", () => {
    const storage = new MemoryStorage();
    storage.enumerationEnabled = false;
    const session = makeSession();
    let conversation = makeConversation("alpha", "一");
    expect(savePersistedConversationState(makeState("alpha", [conversation]), storage, session)).toEqual({
      ok: true,
      revision: `1.${TEST_TAB_ID}`,
    });
    conversation = { ...conversation, updatedAt: 300 };
    expect(savePersistedConversationState(makeState("alpha", [conversation]), storage, session)).toEqual({
      ok: true,
      revision: `2.${TEST_TAB_ID}`,
    });

    expect(storage.getItem(sessionHeadKeyV3("alpha"))).not.toBeNull();
    expect(runIdleCheckpointGc(storage)).toBe(0);
    expect(() => runIdleCheckpointGc(storage)).not.toThrow();
  });
});

describe("V3 retention and idle orphan GC", () => {
  it("removes orphaned snapshots within budget across runs", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "保留")]), storage, session);
    // 崩溃/失败写入的残留：不被任何 head 引用的孤儿快照。
    const orphans = [
      sessionSnapshotKeyV3("alpha", `7.${TEST_TAB_ID}`),
      sessionSnapshotKeyV3("alpha", `8.${TEST_TAB_ID}`),
      sessionSnapshotKeyV3("alpha", `9.${TEST_TAB_ID}`),
      sessionSnapshotKeyV3("ghost", `1.${TEST_TAB_ID}`),
      sessionSnapshotKeyV3("ghost", `2.${TEST_TAB_ID}`),
    ];
    for (const key of orphans) storage.setItem(key, "{}");

    expect(runIdleCheckpointGc(storage, 2)).toBe(2);
    expect(runIdleCheckpointGc(storage, 2)).toBe(2);
    expect(runIdleCheckpointGc(storage, 2)).toBe(1);
    expect(runIdleCheckpointGc(storage, 2)).toBe(0);

    for (const key of orphans) expect(storage.values.has(key)).toBe(false);
    // 被 head 引用的快照完整保留，会话仍可加载。
    expect(storage.values.has(sessionSnapshotKeyV3("alpha", `1.${TEST_TAB_ID}`))).toBe(true);
    const loaded = loadPersistedConversationState(storage, session);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("保留");
  });
});

describe("V2 to V3 migration", () => {
  it("loads V2 journal data identically, then removes V2 and legacy keys after the first verified V3 commit", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    storage.setItem(conversationStorageKeys.conversations, JSON.stringify([{ ...LEGACY_CONVERSATION, id: "legacy-stale" }]));
    storage.setItem(conversationStorageKeys.currentConversation, "legacy-stale");
    storage.setItem(conversationStorageKeys.legacyMessages, JSON.stringify([{ id: "lm-1", role: "user", content: "更旧", createdAt: 50 }]));
    seedV2Journal(storage, [
      { generation: 1, conversations: [LEGACY_CONVERSATION], currentConversationId: "legacy-1" },
      { generation: 2, conversations: [LEGACY_CONVERSATION], currentConversationId: "legacy-1" },
    ]);

    const loaded = loadPersistedConversationState(storage, session);
    expect(loaded.currentConversationId).toBe("legacy-1");
    expect(loaded.conversations.map((conversation) => conversation.id)).toEqual(["legacy-1"]);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("旧消息");

    // 首次 flush：加载得到的全部会话按脏处理并分片，随后 V2 / legacy 键被清理。
    const committed = savePersistedConversationState(loaded, storage, session);
    expect(committed).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });
    expect(storage.getItem(conversationStorageKeys.sessionHead)).toBeNull();
    expect(storage.values.has(sessionSnapshotKey(1))).toBe(false);
    expect(storage.values.has(sessionSnapshotKey(2))).toBe(false);
    expect(storage.values.has(conversationStorageKeys.conversations)).toBe(false);
    expect(storage.values.has(conversationStorageKeys.currentConversation)).toBe(false);
    // legacyMessages 永不删除（与 4.3.5 语义一致）。
    expect(storage.values.has(conversationStorageKeys.legacyMessages)).toBe(true);
    expect(storage.getItem(sessionHeadKeyV3("legacy-1"))).not.toBeNull();

    const reloaded = loadPersistedConversationState(storage, session);
    expect(reloaded.conversations.map((conversation) => conversation.id)).toEqual(["legacy-1"]);
    expect(reloaded.conversations[0]?.messages[0]?.content).toBe("旧消息");
  });

  it("leaves V2 keys intact when the first V3 commit fails and still loads V2 data", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    seedV2Journal(storage, [
      { generation: 1, conversations: [LEGACY_CONVERSATION], currentConversationId: "legacy-1" },
    ]);
    const loaded = loadPersistedConversationState(storage, session);
    storage.failOnSet = (key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix);

    const failed = savePersistedConversationState(loaded, storage, session);
    expect(failed.ok).toBe(false);
    expect(storage.getItem(conversationStorageKeys.sessionHead)).toBe("1");
    expect(storage.values.has(sessionSnapshotKey(1))).toBe(true);

    const reloaded = loadPersistedConversationState(storage, session);
    expect(reloaded.currentConversationId).toBe("legacy-1");
    expect(reloaded.conversations.map((conversation) => conversation.id)).toEqual(["legacy-1"]);

    // 故障恢复后重试：分片成功，V2 键此刻才允许删除。
    storage.failOnSet = null;
    const retried = savePersistedConversationState(reloaded, storage, session);
    expect(retried.ok).toBe(true);
    expect(storage.getItem(conversationStorageKeys.sessionHead)).toBeNull();
    expect(storage.values.has(sessionSnapshotKey(1))).toBe(false);
  });
});

describe("tab current-conversation selection", () => {
  it("persists the selection in sessionStorage only, never in shared V3 keys", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    savePersistedConversationState(makeState("alpha", [makeConversation("alpha", "一")]), storage, session);

    expect(session.getItem(conversationStorageKeys.currentConversationV3)).toBe("alpha");
    expect([...storage.values.keys()].some((key) => key.includes("current-conversation"))).toBe(false);
    expect(storage.values.has(conversationStorageKeys.currentConversation)).toBe(false);
    for (const value of storage.values.values()) {
      expect(value).not.toContain("currentConversationId");
    }
  });

  it("seeds the tab selection from the V2 checkpoint when migrating", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    seedV2Journal(storage, [
      {
        generation: 1,
        conversations: [LEGACY_CONVERSATION, { ...LEGACY_CONVERSATION, id: "legacy-2", updatedAt: 300 }],
        currentConversationId: "legacy-2",
      },
    ]);

    const loaded = loadPersistedConversationState(storage, session);

    expect(loaded.currentConversationId).toBe("legacy-2");
    expect(session.getItem(conversationStorageKeys.currentConversationV3)).toBe("legacy-2");
  });

  it("keeps the tab's stored selection on plain V3 loads and validates it", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    savePersistedConversationState(
      makeState("alpha", [makeConversation("alpha", "一"), { ...makeConversation("beta", "二"), updatedAt: 300 }]),
      storage,
      session,
    );
    session.setItem(conversationStorageKeys.currentConversationV3, "beta");
    expect(loadPersistedConversationState(storage, session).currentConversationId).toBe("beta");

    session.setItem(conversationStorageKeys.currentConversationV3, "missing");
    expect(loadPersistedConversationState(storage, session).currentConversationId).toBe("beta");
  });

  it("treats a selection-only change as a clean save: no shared V3 keys written", () => {
    const adapter = createConversationPersistenceAdapter();
    const storage = new MemoryStorage();
    const session = makeSession();
    const alpha = makeConversation("alpha", "一");
    const beta = { ...makeConversation("beta", "二"), updatedAt: 300 };
    adapter.save(makeState("alpha", [alpha, beta]), storage, session);
    const keysBefore = [...storage.values.keys()];

    // 仅选中变化（会话对象 identity 未变）：共享存储一个键都不动，只更新 sessionStorage 选中键。
    const result = adapter.save(makeState("beta", [alpha, beta]), storage, session);

    expect(result.ok).toBe(true);
    expect([...storage.values.keys()]).toEqual(keysBefore);
    expect(session.getItem(conversationStorageKeys.currentConversationV3)).toBe("beta");
  });

  it("persistSelection writes only the tab session key and degrades silently when sessionStorage fails", () => {
    const adapter = createConversationPersistenceAdapter();
    const session = makeSession();
    adapter.persistSelection("alpha", session);
    expect(session.getItem(conversationStorageKeys.currentConversationV3)).toBe("alpha");
    // null 选中（新对话）移除选中键。
    adapter.persistSelection(null, session);
    expect(session.getItem(conversationStorageKeys.currentConversationV3)).toBeNull();

    // sessionStorage 不可用时静默降级，绝不抛出。
    const brokenSession: StorageLike = {
      getItem: () => null,
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    expect(() => adapter.persistSelection("alpha", brokenSession)).not.toThrow();
  });
});

describe("storage-unavailable handling", () => {
  it("returns storage-unavailable with a size hint instead of throwing", () => {
    const state = makeState("alpha", [makeConversation("alpha", "内容")]);
    const result = savePersistedConversationState(state, null, null);
    expect(result).toMatchObject({ ok: false, code: "storage-unavailable" });
    if (result.ok) return;
    expect(result.message).toMatch(/\(~\d+ bytes\)$/);
  });
});

function makeAssistantMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "assistant-1",
    role: "assistant",
    content: "写到一半",
    reasoning: "想到一半",
    createdAt: 150,
    phase: "answering",
    streaming: true,
    attachments: [],
    timeline: [],
    systemNotes: [],
    ...overrides,
  };
}

function conversationWith(id: string, messages: readonly ChatMessage[]): Conversation {
  return {
    id,
    title: `会话 ${id}`,
    messages,
    model: "deepseek-v4-pro",
    thinkingEnabled: false,
    createdAt: 100,
    updatedAt: 200,
  };
}

describe("checkpointMessage honest interruption", () => {
  it("marks a streaming mid-answering message as interrupted through save+load, preserving partial output", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const user = makeConversation("alpha", "问题").messages[0];
    const state = makeState("alpha", [conversationWith("alpha", [user, makeAssistantMessage()])]);

    expect(savePersistedConversationState(state, storage, session)).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });

    const loaded = loadPersistedConversationState(storage, session);
    const recovered = loaded.conversations[0]?.messages[1];
    expect(recovered).toMatchObject({
      streaming: false,
      phase: "interrupted",
      interrupted: true,
      content: "写到一半",
      reasoning: "想到一半",
    });
    expect(recovered?.systemNotes).toEqual([INTERRUPTED_CHECKPOINT_NOTE]);
    // interrupted === true ⇒ 恢复后的会话可走"继续生成"入口。

    // 重复保存加载后的状态：V3 加载的分片已按"已提交"登记（内容与共享 head
    // 一致），没有脏分片可写，revision 不再空转推进；系统说明仍然只出现一次。
    const reloadedState = makeState(loaded.currentConversationId, [...loaded.conversations]);
    expect(savePersistedConversationState(reloadedState, storage, session)).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });
    const reloaded = loadPersistedConversationState(storage, session);
    const again = reloaded.conversations[0]?.messages[1];
    expect(again).toMatchObject({ streaming: false, phase: "interrupted", interrupted: true });
    expect(again?.systemNotes).toEqual([INTERRUPTED_CHECKPOINT_NOTE]);
  });

  it("normalizes thinking/searching/tool phases but never mislabels completed or user messages", () => {
    for (const phase of ["thinking", "searching", "tool", "answering"] as const) {
      const checkpointed = checkpointMessage(makeAssistantMessage({ phase, streaming: false }));
      expect(checkpointed).toMatchObject({ streaming: false, phase: "interrupted", interrupted: true });
      expect(checkpointed.systemNotes).toEqual([INTERRUPTED_CHECKPOINT_NOTE]);
    }

    for (const phase of ["idle", "done", "error", "interrupted"] as const) {
      const settled = makeAssistantMessage({ phase, streaming: false, interrupted: phase === "interrupted" });
      const checkpointed = checkpointMessage(settled);
      expect(checkpointed.phase).toBe(phase);
      expect(checkpointed.streaming).toBe(false);
      expect(checkpointed.systemNotes).toEqual([]);
      if (phase !== "interrupted") expect(checkpointed.interrupted).not.toBe(true);
    }

    const user = makeConversation("alpha", "用户消息").messages[0];
    expect(checkpointMessage(user)).toEqual({ ...user, streaming: false });
  });

  it("normalizes legacy-shaped in-flight data (phase answering, streaming false) at load", () => {
    const storage = new MemoryStorage();
    storage.setItem(conversationStorageKeys.conversations, JSON.stringify([{
      id: "legacy-1",
      title: "Legacy",
      messages: [{
        id: "assistant-1",
        role: "assistant",
        content: "半截回答",
        reasoning: "半截推理",
        phase: "answering",
        streaming: false,
        interrupted: false,
        createdAt: 100,
      }],
      createdAt: 100,
      updatedAt: 200,
    }]));

    const state = loadPersistedConversationState(storage);
    const message = state.conversations[0]?.messages[0];
    expect(message).toMatchObject({
      streaming: false,
      phase: "interrupted",
      interrupted: true,
      content: "半截回答",
      reasoning: "半截推理",
    });
    expect(message?.systemNotes).toEqual([INTERRUPTED_CHECKPOINT_NOTE]);
  });

  it("never locally interrupts an active agent run; its id and cursor survive save+load", () => {
    const storage = new MemoryStorage();
    const session = makeSession();
    const agentMessage = makeAssistantMessage({
      id: "assistant-run",
      phase: "agent",
      agentRunId: "run_x",
      agentRunStatus: "running",
      agentRunLastEventIndex: 41,
    });
    const state = makeState("alpha", [conversationWith("alpha", [agentMessage])]);

    expect(savePersistedConversationState(state, storage, session)).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });
    const loaded = loadPersistedConversationState(storage, session);
    const recovered = loaded.conversations[0]?.messages[0];
    expect(recovered).toMatchObject({
      agentRunId: "run_x",
      agentRunStatus: "running",
      agentRunLastEventIndex: 41,
      phase: "agent",
      streaming: false,
    });
    expect(recovered?.interrupted).not.toBe(true);
    expect(recovered?.systemNotes).toEqual([]);
  });

  it("exempts an active run even in an in-flight phase, but checkpoints a terminal one like any other", () => {
    const active = checkpointMessage(makeAssistantMessage({
      phase: "answering",
      agentRunId: "run_x",
      agentRunStatus: "running",
    }));
    expect(active).toMatchObject({ phase: "answering", streaming: false });
    expect(active.interrupted).not.toBe(true);

    const terminal = checkpointMessage(makeAssistantMessage({
      phase: "answering",
      agentRunId: "run_x",
      agentRunStatus: "failed",
    }));
    expect(terminal).toMatchObject({ phase: "interrupted", streaming: false, interrupted: true });
    expect(terminal.systemNotes).toEqual([INTERRUPTED_CHECKPOINT_NOTE]);
  });
});

describe("tab identity", () => {
  it("creates a stable 8-hex tab id in sessionStorage", () => {
    const session = new MemoryStorage();
    const first = getTabId(session);
    expect(first).toMatch(/^[0-9a-f]{8}$/);
    expect(session.getItem(TAB_ID_STORAGE_KEY)).toBe(first);
    expect(getTabId(session)).toBe(first);
  });

  it("honors a pre-seeded tab id", () => {
    const session = makeSession("0badf00d");
    expect(getTabId(session)).toBe("0badf00d");
  });
});


describe("storage-pressure compaction", () => {
  const BIG_PREVIEW = `data:image/png;base64,${"A".repeat(64 * 1024)}`;
  const BIG_PAYLOAD_BLOB = "P".repeat(32 * 1024);
  const PREVIEW_BYTES = new TextEncoder().encode(BIG_PREVIEW).length;
  const PAYLOAD_BYTES = new TextEncoder().encode(JSON.stringify({ blob: BIG_PAYLOAD_BLOB })).length;

  function richConversation(id: string): Conversation {
    return {
      id,
      title: `会话 ${id}`,
      messages: [
        {
          ...makeMessage(`${id}-user`, "用户正文一字不动"),
          attachments: [{
            id: "att-1",
            name: "photo.png",
            type: "image/png",
            kind: "image",
            size: 65_536,
            fileId: "file-1",
            preview: BIG_PREVIEW,
            text: "图片 OCR 文本",
          }],
        },
        {
          ...makeAssistantMessage({ id: `${id}-assistant`, content: "助手正文也不动", phase: "done", streaming: false }),
          timeline: [
            { type: "agent", id: "step-1", status: "done", output: "可见输出", payload: { blob: BIG_PAYLOAD_BLOB } },
            { type: "text", text: "小步骤", payload: { small: true } },
          ],
        },
      ],
      model: "deepseek-v4-pro",
      thinkingEnabled: false,
      createdAt: 100,
      updatedAt: 200,
    };
  }

  /** 模拟真实配额：序列化快照超过 limit 字节的写入抛 QuotaExceededError。 */
  function quotaAbove(storage: MemoryStorage, limitBytes: number): void {
    storage.quotaOnSet = (key, value) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix) && value.length > limitBytes;
  }

  function readCheckpoint(storage: MemoryStorage, conversationId: string, revision: string): ConversationCheckpointV3 {
    return JSON.parse(storage.getItem(sessionSnapshotKeyV3(conversationId, revision)) ?? "{}") as ConversationCheckpointV3;
  }

  it("keeps data: previews and records no compaction when the first write fits (4.3.5 contract)", () => {
    const storage = new MemoryStorage();
    const session = makeSession();

    expect(savePersistedConversationState(makeState("alpha", [richConversation("alpha")]), storage, session))
      .toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });

    const checkpoint = readCheckpoint(storage, "alpha", `1.${TEST_TAB_ID}`);
    expect(checkpoint.compaction).toBeUndefined();
    expect(checkpoint.conversation.messages[0]?.attachments[0]?.preview).toBe(BIG_PREVIEW);
    expect(checkpoint.conversation.messages[1]?.timeline[0]?.payload).toEqual({ blob: BIG_PAYLOAD_BLOB });
  });

  it("retries with level-1 compaction on quota: previews stripped, metadata + bodies intact, head advanced, notice surfaced", () => {
    const adapter = createConversationPersistenceAdapter();
    const storage = new MemoryStorage();
    const session = makeSession();
    quotaAbove(storage, 40_000); // 原始快照（~100KB）超限，剥离预览后（~33KB）通过。

    const compactions: ConversationCompactionSignal[] = [];
    const result = adapter.save(makeState("alpha", [richConversation("alpha")]), storage, session, {
      onCompaction: (signal) => compactions.push(signal),
    });

    expect(result).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });
    const head = JSON.parse(storage.getItem(sessionHeadKeyV3("alpha")) ?? "{}") as Record<string, unknown>;
    expect(head.revision).toBe(`1.${TEST_TAB_ID}`);

    const checkpoint = readCheckpoint(storage, "alpha", `1.${TEST_TAB_ID}`);
    expect(checkpoint.digest).toBe(checkpointDigest(JSON.stringify(checkpoint.conversation)));
    expect(checkpoint.compaction).toEqual({ level: 1, removedPreviewBytes: PREVIEW_BYTES, reason: "storage-pressure" });

    // 附件元信息完整，预览负载已剥离。
    const attachment = checkpoint.conversation.messages[0]?.attachments[0];
    expect(attachment).toMatchObject({ id: "att-1", name: "photo.png", type: "image/png", size: 65_536, fileId: "file-1", text: "图片 OCR 文本" });
    expect(attachment?.preview).toBeUndefined();
    // 消息正文逐字节不动；level 1 不触碰 timeline。
    expect(checkpoint.conversation.messages[0]?.content).toBe("用户正文一字不动");
    expect(checkpoint.conversation.messages[1]?.content).toBe("助手正文也不动");
    expect(checkpoint.conversation.messages[1]?.timeline[0]?.payload).toEqual({ blob: BIG_PAYLOAD_BLOB });

    expect(compactions).toEqual([{ conversationId: "alpha", revision: `1.${TEST_TAB_ID}`, level: 1, removedPreviewBytes: PREVIEW_BYTES }]);
  });

  it("escalates to level 2 when quota persists: oversized timeline raw payloads capped, visible state preserved", () => {
    const adapter = createConversationPersistenceAdapter();
    const storage = new MemoryStorage();
    const session = makeSession();
    quotaAbove(storage, 10_000); // level 1（~33KB）仍超限，level 2（~2KB）通过。
    expect(PAYLOAD_BYTES).toBeGreaterThan(TIMELINE_RAW_PAYLOAD_CAP_BYTES);

    const compactions: ConversationCompactionSignal[] = [];
    const result = adapter.save(makeState("alpha", [richConversation("alpha")]), storage, session, {
      onCompaction: (signal) => compactions.push(signal),
    });

    expect(result).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });
    const checkpoint = readCheckpoint(storage, "alpha", `1.${TEST_TAB_ID}`);
    expect(checkpoint.digest).toBe(checkpointDigest(JSON.stringify(checkpoint.conversation)));
    expect(checkpoint.compaction).toEqual({ level: 2, removedPreviewBytes: PREVIEW_BYTES, reason: "storage-pressure" });

    const steps = checkpoint.conversation.messages[1]?.timeline ?? [];
    // 超大原始 payload 以有界标记替换；step 的最终状态与可见输出完整保留。
    expect(steps[0]?.payload).toEqual({ compacted: true, originalBytes: PAYLOAD_BYTES });
    expect(steps[0]).toMatchObject({ type: "agent", id: "step-1", status: "done", output: "可见输出" });
    // 低于上限的 payload 不动。
    expect(steps[1]?.payload).toEqual({ small: true });
    expect(compactions).toEqual([{ conversationId: "alpha", revision: `1.${TEST_TAB_ID}`, level: 2, removedPreviewBytes: PREVIEW_BYTES }]);
  });

  it("returns storage-pressure when every level still exceeds quota: old head/snapshot untouched, nothing deleted, freed retry commits", () => {
    const adapter = createConversationPersistenceAdapter();
    const storage = new MemoryStorage();
    const session = makeSession();
    const base = richConversation("alpha");
    expect(adapter.save(makeState("alpha", [base]), storage, session)).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });

    const edited: Conversation = { ...base, messages: [...base.messages, makeMessage("alpha-edit", "新增一条")], updatedAt: 300 };
    quotaAbove(storage, 0); // 所有快照写入都超限。
    const compactions: ConversationCompactionSignal[] = [];
    const failed = adapter.save(makeState("alpha", [edited]), storage, session, {
      onCompaction: (signal) => compactions.push(signal),
    });

    expect(failed.ok).toBe(false);
    if (failed.ok) return;
    expect(failed.code).toBe("storage-pressure");
    expect(failed.message).toContain("会话 alpha");
    expect(compactions).toEqual([]);

    // 旧 head 与旧快照原样保留：加载仍返回上一修订，新内容未撕裂写入。
    const head = JSON.parse(storage.getItem(sessionHeadKeyV3("alpha")) ?? "{}") as Record<string, unknown>;
    expect(head.revision).toBe(`1.${TEST_TAB_ID}`);
    expect(v3SnapshotKeys(storage, "alpha")).toEqual([sessionSnapshotKeyV3("alpha", `1.${TEST_TAB_ID}`)]);
    const loaded = adapter.load(storage, session);
    expect(loaded.conversations[0]?.messages[0]?.content).toBe("用户正文一字不动");
    expect(loaded.conversations[0]?.messages.some((message) => message.content === "新增一条")).toBe(false);

    // 释放空间后：同一编辑的下次提交按原始形态成功，无需压缩。
    storage.quotaOnSet = null;
    const retried = adapter.save(makeState("alpha", [edited]), storage, session, {
      onCompaction: (signal) => compactions.push(signal),
    });
    expect(retried).toEqual({ ok: true, revision: `2.${TEST_TAB_ID}` });
    expect(compactions).toEqual([]);
    const reloaded = adapter.load(storage, session);
    expect(reloaded.conversations[0]?.messages.some((message) => message.content === "新增一条")).toBe(true);
    expect(reloaded.conversations[0]?.messages[0]?.attachments[0]?.preview).toBe(BIG_PREVIEW);
  });

  it("compactConversationForStorage is deterministic: same conversation + level ⇒ same output and byte counts", () => {
    const conversation = richConversation("alpha");
    for (const level of [1, 2]) {
      const first = compactConversationForStorage(conversation, level);
      const second = compactConversationForStorage(conversation, level);
      expect(first.removedPreviewBytes).toBe(second.removedPreviewBytes);
      expect(first.conversation).toEqual(second.conversation);
      expect(JSON.stringify(first.conversation)).toBe(JSON.stringify(second.conversation));
    }

    const level1 = compactConversationForStorage(conversation, 1);
    expect(level1.removedPreviewBytes).toBe(PREVIEW_BYTES);
    expect(level1.conversation.messages[0]?.attachments[0]?.preview).toBeUndefined();
    // level 1 不触碰 timeline，level 2 是其超集（预览同样剥离，仅超大 payload 被设上限）。
    expect(level1.conversation.messages[1]?.timeline[0]?.payload).toEqual({ blob: BIG_PAYLOAD_BLOB });
    const level2 = compactConversationForStorage(conversation, 2);
    expect(level2.removedPreviewBytes).toBe(PREVIEW_BYTES);
    expect(level2.conversation.messages[1]?.timeline[0]?.payload).toEqual({ compacted: true, originalBytes: PAYLOAD_BYTES });
    expect(level2.conversation.messages[1]?.timeline[1]?.payload).toEqual({ small: true });
    // 正文与附件元信息在任何级别都不动。
    expect(level2.conversation.messages[0]?.content).toBe("用户正文一字不动");
    expect(level2.conversation.messages[1]?.content).toBe("助手正文也不动");
    // 低于剥离阈值的小预览保留。
    expect(PREVIEW_COMPACTION_MIN_BYTES).toBeGreaterThan(0);
    const smallPreview: Conversation = {
      ...conversation,
      messages: [{ ...conversation.messages[0] as ChatMessage, attachments: [{ name: "icon.png", preview: "data:image/png;base64,tiny" }] }],
    };
    expect(compactConversationForStorage(smallPreview, 2).conversation.messages[0]?.attachments[0]?.preview)
      .toBe("data:image/png;base64,tiny");
    // 无可压缩内容：返回原对象（identity 保持，不影响脏检测）。
    const plain = makeConversation("plain", "无预览");
    expect(compactConversationForStorage(plain, 2).conversation).toBe(plain);
  });

  it("compaction metadata survives the save→load round-trip; compacted conversations load and render normally", () => {
    const adapter = createConversationPersistenceAdapter();
    const storage = new MemoryStorage();
    const session = makeSession();
    quotaAbove(storage, 10_000); // level 2 落盘。

    const saved = adapter.save(makeState("alpha", [richConversation("alpha")]), storage, session);
    expect(saved).toEqual({ ok: true, revision: `1.${TEST_TAB_ID}` });

    const loaded = adapter.load(storage, session);
    const conversation = loaded.conversations[0];
    expect(conversation?.messages[0]?.content).toBe("用户正文一字不动");
    expect(conversation?.messages[1]?.content).toBe("助手正文也不动");
    // 加载容忍缺失预览：名称 / 类型 / 大小 / fileId / 文本完整（MessageItem 回退到名称 + 类型渲染）。
    const attachment = conversation?.messages[0]?.attachments[0];
    expect(attachment).toMatchObject({ name: "photo.png", type: "image/png", kind: "image", size: 65_536, fileId: "file-1", text: "图片 OCR 文本" });
    expect(attachment?.preview).toBeUndefined();
    expect(conversation?.messages[1]?.timeline[0]?.output).toBe("可见输出");

    // 压缩记录仍在磁盘上的 checkpoint 信封里（加载不重写快照）。
    const checkpoint = readCheckpoint(storage, "alpha", `1.${TEST_TAB_ID}`);
    expect(checkpoint.compaction).toEqual({ level: 2, removedPreviewBytes: PREVIEW_BYTES, reason: "storage-pressure" });
  });
});
