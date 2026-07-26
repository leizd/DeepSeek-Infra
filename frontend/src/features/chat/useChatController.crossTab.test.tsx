// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatStreamEvent } from "../../domain/chat/types";

const { streamChatMock, settingsStub } = vi.hoisted(() => ({
  streamChatMock: vi.fn(),
  settingsStub: {
    apiKey: "sk-test",
    tavilyApiKey: "",
    model: "deepseek-chat",
    thinkingEnabled: false,
    searchEnabled: false,
    agentMode: false,
    agentPreset: "full",
    memoryEnabled: true,
    runtime: null as null | { hasServerKey: boolean },
  },
}));

vi.mock("../../api/chatStream", () => ({ streamChat: streamChatMock }));
vi.mock("../../api/titleApi", () => ({ generateConversationTitle: vi.fn(() => Promise.resolve("")) }));
vi.mock("../../api/remindersApi", () => ({ createReminder: vi.fn(() => Promise.resolve()) }));
vi.mock("../../contexts/SettingsContext", () => ({
  useSettings: () => settingsStub,
}));
vi.mock("../../contexts/ProjectsContext", () => ({
  useProjects: () => ({ chatContext: () => ({ projectAttachments: [] }) }),
}));
vi.mock("../../contexts/MemoryContext", () => ({
  useMemory: () => ({ save: vi.fn(() => Promise.resolve({ saved: true, conflicts: [] })) }),
}));
vi.mock("../agent-run/useAgentRun", () => ({
  useAgentRun: () => ({
    sendAgentMessage: vi.fn(() => Promise.resolve()),
    confirmPlan: vi.fn(() => Promise.resolve()),
    rerunPhase: vi.fn(() => Promise.resolve()),
  }),
}));
vi.mock("../reminders/useReminderPolling", () => ({
  ensureNotificationPermission: vi.fn(() => Promise.resolve()),
}));

import { useChatController } from "./useChatController";
import {
  getPersistenceHealthSnapshot,
  resetPersistenceHealthForTests,
} from "../../app/persistenceHealth";
import { TAB_ID_STORAGE_KEY } from "../../app/tabIdentity";
import {
  conversationStorageKeys,
  createConversationPersistenceAdapter,
  sessionConflictKeyV3,
  sessionHeadKeyV3,
  sessionTombstoneKeyV3,
  type ConversationPersistenceAdapter,
  type LockRequestOptions,
  type LocksLike,
  type StorageLike,
} from "../../domain/conversation/persistence";
import type { Conversation } from "../../domain/conversation/types";
import type { ChatMessage } from "../../domain/chat/types";
import type { ConversationSyncChannel, ConversationSyncMessage } from "../../app/conversationSync";

const TAB_A = "aaaa0001";
const TAB_B = "bbbb0002";
// 测试用显式 flush 驱动保存，防抖调到不会触发的大小。
const NO_AUTOSAVE = 3_600_000;

class MemorySession implements StorageLike {
  readonly values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

function makeSession(tabId: string): MemorySession {
  const session = new MemorySession();
  session.setItem(TAB_ID_STORAGE_KEY, tabId);
  return session;
}

function createChannelPair() {
  const listenersA = new Set<(message: ConversationSyncMessage) => void>();
  const listenersB = new Set<(message: ConversationSyncMessage) => void>();
  const posted: ConversationSyncMessage[] = [];
  const make = (
    mine: Set<(message: ConversationSyncMessage) => void>,
    other: Set<(message: ConversationSyncMessage) => void>,
  ): ConversationSyncChannel => ({
    post: (message) => {
      posted.push(message);
      other.forEach((listener) => listener(message));
    },
    subscribe: (listener) => {
      mine.add(listener);
      return () => {
        mine.delete(listener);
      };
    },
  });
  return { channelA: make(listenersA, listenersB), channelB: make(listenersB, listenersA), posted };
}

/** 闸门锁：release 前所有锁请求都排队等待，用于精确控制仲裁时序。 */
class GatedLock implements LocksLike {
  private open!: () => void;
  private readonly gate = new Promise<void>((resolve) => {
    this.open = resolve;
  });
  request<T>(_name: string, _options: LockRequestOptions, callback: () => T | Promise<T>): Promise<T> {
    return this.gate.then(callback);
  }
  release(): void {
    this.open();
  }
}

/** 排空微任务队列（删除流程经 promise 链落地）。 */
function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
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

interface TabRig {
  adapter: ConversationPersistenceAdapter;
  session: MemorySession;
  channel: ConversationSyncChannel;
}

function doneStream(): AsyncGenerator<ChatStreamEvent> {
  return (async function* stream() {
    yield { type: "done", content: "" };
  })();
}

function gatedStream(): { stream: AsyncGenerator<ChatStreamEvent>; release: () => void } {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const stream = (async function* (): AsyncGenerator<ChatStreamEvent> {
    yield { type: "content", text: "流式片段" };
    await gate;
    yield { type: "done", content: "" };
  })();
  return { stream, release };
}

function headOf(conversationId: string): Record<string, unknown> {
  return JSON.parse(window.localStorage.getItem(sessionHeadKeyV3(conversationId)) ?? "{}") as Record<string, unknown>;
}

let bus: ReturnType<typeof createChannelPair>;
let tabA: TabRig;
let tabB: TabRig;

function mountTab(rig: TabRig) {
  return renderHook(() => useChatController({
    persistence: rig.adapter,
    syncChannel: rig.channel,
    session: rig.session,
    autosaveDebounceMs: NO_AUTOSAVE,
  }));
}

/** 用标签页 A 的适配器播种共享 localStorage（此时两个控制器都还没挂载）。 */
function seedConversations(conversations: Conversation[]): void {
  tabA.adapter.save(
    { schemaVersion: 1, currentConversationId: conversations[0]?.id ?? null, conversations },
    window.localStorage,
    tabA.session,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  streamChatMock.mockReset();
  settingsStub.apiKey = "sk-test";
  settingsStub.runtime = null;
  settingsStub.agentMode = false;
  bus = createChannelPair();
  tabA = { adapter: createConversationPersistenceAdapter(), session: makeSession(TAB_A), channel: bus.channelA };
  tabB = { adapter: createConversationPersistenceAdapter(), session: makeSession(TAB_B), channel: bus.channelB };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  resetPersistenceHealthForTests();
});

describe("cross-tab conversation sync", () => {
  it("posts conversation_committed to the channel on every successful commit", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const a = mountTab(tabA);
    mountTab(tabB);

    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await a.result.current.sendMessage("来自 A 的消息");
    });
    act(() => {
      a.result.current.flushConversationPersistence();
    });

    expect(bus.posted).toEqual([
      expect.objectContaining({
        type: "conversation_committed",
        conversationId: "alpha",
        revision: `2.${TAB_A}`,
        writerId: TAB_A,
      }),
    ]);
  });

  it("commits a tombstone before removing locally; a clean receiver drops the conversation", async () => {
    seedConversations([makeConversation("alpha", "一"), makeConversation("beta", "二")]);
    const a = mountTab(tabA);
    const b = mountTab(tabB);

    await act(async () => {
      a.result.current.deleteConversation("beta");
      await settle();
    });

    // tombstone 先于 UI 移除耐久落盘：记录被删 head 的 parentRevision。
    expect(JSON.parse(window.localStorage.getItem(sessionTombstoneKeyV3("beta")) ?? "null")).toMatchObject({
      conversationId: "beta",
      deletedRevision: `2.${TAB_A}`,
      parentRevision: `1.${TAB_A}`,
      writerId: TAB_A,
    });
    expect(window.localStorage.getItem(sessionHeadKeyV3("beta"))).toBeNull();
    expect(bus.posted).toEqual([
      { type: "conversation_deleted", conversationId: "beta", writerId: TAB_A },
    ]);
    // 删除方 UI 移除（选中回退保持既有 UX）。
    expect(a.result.current.state.conversations.map((conversation) => conversation.id)).toEqual(["alpha"]);
    expect(a.result.current.state.currentConversationId).toBe("alpha");
    // 干净接收端同样移除，且绝不切换自己正在看的会话。
    expect(b.result.current.state.conversations.map((conversation) => conversation.id)).toEqual(["alpha"]);
    expect(b.result.current.state.currentConversationId).toBe("alpha");
  });

  it("keeps the conversation in the UI when the tombstone write fails", async () => {
    seedConversations([makeConversation("alpha", "一"), makeConversation("beta", "二")]);
    const a = mountTab(tabA);

    const originalSet = Object.getOwnPropertyDescriptor(Storage.prototype, "setItem")?.value as (
      this: Storage,
      key: string,
      value: string,
    ) => void;
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (key.startsWith(conversationStorageKeys.v3TombstonePrefix)) throw new Error("disk full");
      originalSet.call(this, key, value);
    });

    await act(async () => {
      a.result.current.deleteConversation("beta");
      await settle();
    });

    // 会话保留在 UI，head 完好，未广播删除；失败经 flush 链路记录并提示。
    expect(a.result.current.state.conversations.some((conversation) => conversation.id === "beta")).toBe(true);
    expect(window.localStorage.getItem(sessionHeadKeyV3("beta"))).not.toBeNull();
    expect(window.localStorage.getItem(sessionTombstoneKeyV3("beta"))).toBeNull();
    expect(bus.posted).toEqual([]);
    expect(a.result.current.state.notice).toContain("会话 beta");
    expect(getPersistenceHealthSnapshot().failedIds).toEqual(["conversation"]);
    setItem.mockRestore();

    // 恢复后重试成功。
    await act(async () => {
      a.result.current.deleteConversation("beta");
      await settle();
    });
    expect(a.result.current.state.conversations.some((conversation) => conversation.id === "beta")).toBe(false);
    expect(window.localStorage.getItem(sessionTombstoneKeyV3("beta"))).not.toBeNull();
  });

  it("removes the conversation from the UI only after the tombstone is durably written", async () => {
    const gated = new GatedLock();
    const rigA: TabRig = {
      adapter: createConversationPersistenceAdapter({ locks: gated }),
      session: makeSession(TAB_A),
      channel: bus.channelA,
    };
    rigA.adapter.save(
      { schemaVersion: 1, currentConversationId: "beta", conversations: [makeConversation("alpha", "一"), makeConversation("beta", "二")] },
      window.localStorage,
      rigA.session,
    );
    const a = mountTab(rigA);

    act(() => {
      a.result.current.deleteConversation("beta");
    });
    // 锁未放行：tombstone 未落盘，UI 原样保留。
    expect(window.localStorage.getItem(sessionTombstoneKeyV3("beta"))).toBeNull();
    expect(a.result.current.state.conversations.some((conversation) => conversation.id === "beta")).toBe(true);

    await act(async () => {
      gated.release();
      await settle();
    });
    // tombstone 耐久后才移除 UI。
    expect(window.localStorage.getItem(sessionTombstoneKeyV3("beta"))).not.toBeNull();
    expect(window.localStorage.getItem(sessionHeadKeyV3("beta"))).toBeNull();
    expect(a.result.current.state.conversations.some((conversation) => conversation.id === "beta")).toBe(false);
  });

  it("a dirty receiver keeps the content and materializes a recovery copy on next commit", async () => {
    seedConversations([makeConversation("alpha", "一"), makeConversation("beta", "二")]);
    const a = mountTab(tabA);
    const b = mountTab(tabB);

    // B 对 beta 有未提交修改（重命名产生脏对象，NO_AUTOSAVE 下不会落盘）。
    act(() => {
      b.result.current.renameConversation("beta", "B 的标题");
    });
    await act(async () => {
      a.result.current.deleteConversation("beta");
      await settle();
    });

    // 本地脏 ⇒ 保留内容，绝不静默丢弃。
    expect(b.result.current.state.conversations.find((conversation) => conversation.id === "beta")?.title).toBe("B 的标题");

    act(() => {
      b.result.current.flushConversationPersistence();
    });

    // 下一次提交被 tombstone 拒绝：原 cid 移除，内容以恢复副本幸存并提示。
    expect(b.result.current.state.conversations.some((conversation) => conversation.id === "beta")).toBe(false);
    const copy = b.result.current.state.conversations.find((conversation) => conversation.title === "B 的标题（恢复副本）");
    expect(copy).toBeTruthy();
    expect(copy?.id).not.toBe("beta");
    expect(b.result.current.state.notice).toContain("恢复副本");
    expect(window.localStorage.getItem(sessionHeadKeyV3("beta"))).toBeNull();
    expect(window.localStorage.getItem(sessionTombstoneKeyV3("beta"))).not.toBeNull();
    expect(window.localStorage.getItem(sessionHeadKeyV3(copy?.id as string))).not.toBeNull();
  });

  it("a stale tab that missed the delete entirely is refused on commit and keeps a recovery copy", async () => {
    // B 的通道丢弃所有消息（模拟完全没收到删除通知的标签页）。
    const blackhole: ConversationSyncChannel = { post: () => undefined, subscribe: () => () => undefined };
    const rigB: TabRig = { adapter: createConversationPersistenceAdapter(), session: makeSession(TAB_B), channel: blackhole };
    seedConversations([makeConversation("alpha", "初版")]);
    const b = mountTab(rigB);
    const a = mountTab(tabA);

    act(() => {
      b.result.current.renameConversation("alpha", "B 的标题");
    });
    await act(async () => {
      a.result.current.deleteConversation("alpha");
      await settle();
    });

    act(() => {
      b.result.current.flushConversationPersistence();
    });

    expect(b.result.current.state.conversations.some((conversation) => conversation.id === "alpha")).toBe(false);
    const copy = b.result.current.state.conversations.find((conversation) => conversation.title === "B 的标题（恢复副本）");
    expect(copy).toBeTruthy();
    expect(b.result.current.state.notice).toContain("恢复副本");
    expect(window.localStorage.getItem(sessionHeadKeyV3("alpha"))).toBeNull();
    // 原 cid 在后续加载中绝不复活。
    const reloaded = rigB.adapter.load(window.localStorage, rigB.session);
    expect(reloaded.conversations.map((conversation) => conversation.id)).toEqual([copy?.id]);
  });

  it("reloads a clean remote-committed conversation without ever switching selection", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const a = mountTab(tabA);
    const b = mountTab(tabB);
    // B 正在看"新对话"：远端同步绝不能把它拽回 alpha。
    act(() => {
      b.result.current.newConversation();
    });
    expect(b.result.current.state.currentConversationId).toBeNull();

    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await a.result.current.sendMessage("来自 A 的消息");
    });
    act(() => {
      a.result.current.flushConversationPersistence();
    });

    const alpha = b.result.current.state.conversations.find((conversation) => conversation.id === "alpha");
    expect(alpha?.messages.some((message) => message.content === "来自 A 的消息")).toBe(true);
    expect(b.result.current.state.currentConversationId).toBeNull();
  });

  it("adds an unknown remote conversation via the normal load path without switching selection", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const a = mountTab(tabA);
    const b = mountTab(tabB);

    act(() => {
      a.result.current.newConversation();
    });
    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await a.result.current.sendMessage("A 的新会话");
    });
    act(() => {
      a.result.current.flushConversationPersistence();
    });

    const created = a.result.current.state.currentConversationId as string;
    expect(created).not.toBe("alpha");
    expect(bus.posted.some((message) => message.type === "conversation_committed" && message.conversationId === created)).toBe(true);
    expect(b.result.current.state.conversations.some((conversation) => conversation.id === created)).toBe(true);
    expect(b.result.current.state.currentConversationId).toBe("alpha");
  });

  async function produceConflict() {
    const a = mountTab(tabA);
    const b = mountTab(tabB);
    // B 本地修改 alpha（未 flush）；A 也修改并提交 → B 成为落后方。
    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await b.result.current.sendMessage("B 的修改");
    });
    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await a.result.current.sendMessage("A 的修改");
    });
    act(() => {
      a.result.current.flushConversationPersistence();
    });
    act(() => {
      b.result.current.flushConversationPersistence();
    });
    return { a, b };
  }

  it("a dirty tab conflict-branches on commit: head intact, pointer written, notice surfaced", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const { b } = await produceConflict();

    expect(headOf("alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(window.localStorage.getItem(sessionConflictKeyV3("alpha"))).not.toBeNull();
    expect(b.result.current.conflict).toMatchObject({
      conversationId: "alpha",
      title: "初版",
      sharedRevision: `2.${TAB_A}`,
      writerId: TAB_B,
    });
    // 本地内容不丢弃；选中未被切换。
    const alpha = b.result.current.state.conversations.find((conversation) => conversation.id === "alpha");
    expect(alpha?.messages.some((message) => message.content === "B 的修改")).toBe(true);
    expect(b.result.current.state.currentConversationId).toBe("alpha");
  });

  it("保留副本 materializes the conflict branch as an independent conversation and clears the pointer", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const { b } = await produceConflict();

    act(() => {
      b.result.current.resolveConflictByCopy();
    });

    expect(b.result.current.conflict).toBeNull();
    expect(window.localStorage.getItem(sessionConflictKeyV3("alpha"))).toBeNull();
    const copy = b.result.current.state.conversations.find((conversation) => conversation.title.endsWith("（冲突副本）"));
    expect(copy).toBeTruthy();
    expect(copy?.id).not.toBe("alpha");
    expect(copy?.messages.some((message) => message.content === "B 的修改")).toBe(true);
    expect(b.result.current.state.currentConversationId).toBe("alpha");

    // 副本作为它自己的分片提交。
    act(() => {
      b.result.current.flushConversationPersistence();
    });
    expect(window.localStorage.getItem(sessionHeadKeyV3(copy?.id as string))).not.toBeNull();
    expect(headOf("alpha")).toMatchObject({ revision: `2.${TAB_A}` });
  });

  it("查看最新 reloads the shared head and clears the pointer", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const { b } = await produceConflict();

    act(() => {
      b.result.current.resolveConflictByReload();
    });

    expect(b.result.current.conflict).toBeNull();
    expect(window.localStorage.getItem(sessionConflictKeyV3("alpha"))).toBeNull();
    const alpha = b.result.current.state.conversations.find((conversation) => conversation.id === "alpha");
    expect(alpha?.messages.some((message) => message.content === "A 的修改")).toBe(true);
    expect(alpha?.messages.some((message) => message.content === "B 的修改")).toBe(false);
    expect(b.result.current.state.currentConversationId).toBe("alpha");

    // 换入的副本已登记为已提交：后续 flush 不重写 head。
    act(() => {
      b.result.current.flushConversationPersistence();
    });
    expect(headOf("alpha")).toMatchObject({ revision: `2.${TAB_A}` });
  });

  it("conflict-branch write failure surfaces write-conflict through the flusher into health", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const a = mountTab(tabA);
    const b = mountTab(tabB);
    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await b.result.current.sendMessage("B 的修改");
    });
    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await a.result.current.sendMessage("A 的修改");
    });
    act(() => {
      a.result.current.flushConversationPersistence();
    });

    const originalSet = Object.getOwnPropertyDescriptor(Storage.prototype, "setItem")?.value as (
      this: Storage,
      key: string,
      value: string,
    ) => void;
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (key.startsWith(conversationStorageKeys.v3SnapshotPrefix)) throw new Error("disk full");
      originalSet.call(this, key, value);
    });

    let flush: ReturnType<typeof b.result.current.flushConversationPersistence> | undefined;
    act(() => {
      flush = b.result.current.flushConversationPersistence();
    });

    expect(flush).toMatchObject({ ok: false, code: "write-conflict" });
    expect(getPersistenceHealthSnapshot().failedIds).toEqual(["conversation"]);
    expect(getPersistenceHealthSnapshot().lastErrors.conversation?.code).toBe("write-conflict");
    expect(headOf("alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    setItem.mockRestore();
  });

  it("does not interrupt an actively streaming conversation; the stale base conflict-branches after settle", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const a = mountTab(tabA);
    const b = mountTab(tabB);

    const gated = gatedStream();
    streamChatMock.mockImplementationOnce(() => gated.stream);
    act(() => {
      b.result.current.tryStartMessage("B 的流式问题");
    });
    expect(b.result.current.state.requestStatus).toBe("streaming");

    // 远端提交到达时 B 正在流式：刷新被推迟，流式内容原样保留。
    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await a.result.current.sendMessage("A 的修改");
    });
    act(() => {
      a.result.current.flushConversationPersistence();
    });

    let alpha = b.result.current.state.conversations.find((conversation) => conversation.id === "alpha");
    expect(b.result.current.state.requestStatus).toBe("streaming");
    expect(alpha?.messages.at(-1)?.streaming).toBe(true);
    expect(alpha?.messages.some((message) => message.content === "A 的修改")).toBe(false);
    expect(b.result.current.state.currentConversationId).toBe("alpha");

    // 流结束后：本地是脏的（有自己的新消息），对账标记 stale 而不是换入；
    // 下一次 flush 走冲突分支，head 不被覆盖，本地内容完整保留。
    await act(async () => {
      gated.release();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(b.result.current.state.requestStatus).toBe("idle");
    alpha = b.result.current.state.conversations.find((conversation) => conversation.id === "alpha");
    expect(alpha?.messages.some((message) => message.content === "B 的流式问题")).toBe(true);

    act(() => {
      b.result.current.flushConversationPersistence();
    });
    expect(headOf("alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(window.localStorage.getItem(sessionConflictKeyV3("alpha"))).not.toBeNull();
    expect(b.result.current.conflict).toMatchObject({ conversationId: "alpha" });
    expect(b.result.current.state.currentConversationId).toBe("alpha");
  });

  it("ignores channel messages carrying its own writerId", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const b = mountTab(tabB);
    const before = b.result.current.state.conversations.find((conversation) => conversation.id === "alpha");

    // 模拟一条回到本标签页的广播（如对端回声）：writerId 是自己 → 直接忽略。
    act(() => {
      bus.channelA.post({
        type: "conversation_committed",
        conversationId: "alpha",
        revision: `9.${TAB_B}`,
        writerId: TAB_B,
        savedAt: Date.now(),
      });
    });

    const after = b.result.current.state.conversations.find((conversation) => conversation.id === "alpha");
    expect(after).toBe(before);
  });
});
