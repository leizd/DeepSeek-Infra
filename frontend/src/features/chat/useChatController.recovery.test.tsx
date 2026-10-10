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
  parseRecoveryCapsule,
  recoveredCopyIdV3,
  sessionHeadKeyV3,
  sessionRecoveryKeyV3,
  sessionSnapshotKeyV3,
  sessionTombstoneKeyV3,
  type ConversationPersistenceAdapter,
  type StorageLike,
} from "../../domain/conversation/persistence";
import type { Conversation } from "../../domain/conversation/types";
import type { ChatMessage } from "../../domain/chat/types";
import type { ConversationSyncChannel, ConversationSyncMessage } from "../../app/conversationSync";

const TAB_A = "aaaa0001";
const TAB_B = "bbbb0002";
const TAB_C = "cccc0003";
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

/** 排空微任务队列（启动对账经 promise 链落地）。 */
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

function headOf(conversationId: string): Record<string, unknown> {
  return JSON.parse(window.localStorage.getItem(sessionHeadKeyV3(conversationId)) ?? "{}") as Record<string, unknown>;
}

function localStorageKeys(): string[] {
  const keys: string[] = [];
  for (let index = 0; index < window.localStorage.length; index += 1) {
    const key = window.localStorage.key(index);
    if (key) keys.push(key);
  }
  return keys;
}

/** 在 jsdom localStorage 上安装写失败 predicate（命中即抛错，其余键正常）。 */
function installSetItemSpy(predicate: (key: string) => boolean) {
  const originalSet = Object.getOwnPropertyDescriptor(Storage.prototype, "setItem")?.value as (
    this: Storage,
    key: string,
    value: string,
  ) => void;
  return vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
    if (this === window.localStorage && predicate(key)) throw new Error("disk full");
    originalSet.call(this, key, value);
  });
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

/** 用标签页 A 的适配器播种共享 localStorage（此时控制器还没挂载）。 */
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
  tabA = {
    adapter: createConversationPersistenceAdapter({ identity: { writerSessionId: TAB_A, documentInstanceId: "doc-a" } }),
    session: makeSession(TAB_A),
    channel: bus.channelA,
  };
  tabB = {
    adapter: createConversationPersistenceAdapter({ identity: { writerSessionId: TAB_B, documentInstanceId: "doc-b" } }),
    session: makeSession(TAB_B),
    channel: bus.channelB,
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  resetPersistenceHealthForTests();
});

describe("uncommitted-tab recovery capsules", () => {
  it("pagehide with unflushable shards writes a capsule; the next session reconciles it in place", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const a = mountTab(tabA);
    act(() => {
      a.result.current.renameConversation("alpha", "A 的未提交标题");
    });

    // 分片键写不进去，恢复键可用：pagehide 同步 flush 失败 ⇒ 应急胶囊落盘。
    const setItem = installSetItemSpy((key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix));
    let flush: ReturnType<typeof a.result.current.flushConversationPersistence> | undefined;
    act(() => {
      flush = a.result.current.flushConversationPersistence();
    });
    setItem.mockRestore();

    expect(flush).toMatchObject({ ok: false });
    expect(getPersistenceHealthSnapshot().failedIds).toEqual(["conversation"]);
    const capsule = parseRecoveryCapsule(window.localStorage.getItem(sessionRecoveryKeyV3(TAB_A)));
    expect(capsule).toMatchObject({ schemaVersion: 2, writerSessionId: TAB_A });
    expect(capsule?.entries.map((entry) => entry.conversationId)).toEqual(["alpha"]);
    expect(capsule?.entries[0]?.baseRevision).toBe(`1.${TAB_A}`);
    expect(capsule?.entries[0]?.conversation.title).toBe("A 的未提交标题");
    // 胶囊绝不推进共享 head。
    expect(headOf("alpha")).toMatchObject({ revision: `1.${TAB_A}` });
    a.unmount();

    // 下一次会话（浏览器 session restore：同 tabId，新适配器）：锁内对账，干净补交。
    const rigA2: TabRig = {
      adapter: createConversationPersistenceAdapter({ identity: { writerSessionId: TAB_A, documentInstanceId: "doc-a-restored" } }),
      session: tabA.session,
      channel: bus.channelA,
    };
    const a2 = mountTab(rigA2);
    await act(async () => {
      await settle();
    });

    expect(headOf("alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    const alpha = a2.result.current.state.conversations.find((conversation) => conversation.id === "alpha");
    expect(alpha?.title).toBe("A 的未提交标题");
    expect(alpha?.messages[0]?.content).toBe("初版");
    expect(window.localStorage.getItem(sessionRecoveryKeyV3(TAB_A))).toBeNull();
    // 干净补交不需要提示条；内容换入即恢复。
    expect(a2.result.current.state.notice).toBe("");
  });

  it("pagehide after a fully successful flush leaves no capsule key", () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const a = mountTab(tabA);
    // 过期胶囊残留：成功的 flush 必须把它移除。
    window.localStorage.setItem(sessionRecoveryKeyV3(TAB_A), "stale");
    act(() => {
      a.result.current.renameConversation("alpha", "已提交标题");
    });

    let flush: ReturnType<typeof a.result.current.flushConversationPersistence> | undefined;
    act(() => {
      flush = a.result.current.flushConversationPersistence();
    });

    expect(flush).toMatchObject({ ok: true });
    expect(window.localStorage.getItem(sessionRecoveryKeyV3(TAB_A))).toBeNull();
  });

  it("a dirty tab that dies while a sibling advances the head is reclaimed as one deterministic recovery copy", async () => {
    seedConversations([makeConversation("alpha", "初版")]);
    // A 全程在线（先于 B 的胶囊挂载，启动对账不会提前回收它）。
    const a = mountTab(tabA);
    const b = mountTab(tabB);
    act(() => {
      b.result.current.renameConversation("alpha", "B 的未提交标题");
    });
    // B 死亡前 pagehide：分片写不进去 ⇒ 胶囊（base 1.TAB_A）落盘。
    const setItem = installSetItemSpy((key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix));
    act(() => {
      b.result.current.flushConversationPersistence();
    });
    setItem.mockRestore();
    b.unmount();

    // 兄弟标签页 A 推进共享 head：B 的胶囊 base 已过期。
    streamChatMock.mockImplementationOnce(() => doneStream());
    await act(async () => {
      await a.result.current.sendMessage("A 的修改");
    });
    act(() => {
      a.result.current.flushConversationPersistence();
    });
    expect(headOf("alpha")).toMatchObject({ revision: `2.${TAB_A}` });

    // B 会话恢复（同 tabId，新适配器）：head 已推进 ⇒ 确定性恢复副本。
    const rigB2: TabRig = {
      adapter: createConversationPersistenceAdapter({ identity: { writerSessionId: TAB_B, documentInstanceId: "doc-b-restored" } }),
      session: tabB.session,
      channel: bus.channelB,
    };
    const b2 = mountTab(rigB2);
    await act(async () => {
      await settle();
    });

    const copyId = recoveredCopyIdV3("alpha", TAB_B, 1);
    const copy = b2.result.current.state.conversations.find((conversation) => conversation.id === copyId);
    expect(copy).toBeTruthy();
    expect(copy?.title).toBe("B 的未提交标题（恢复副本）");
    expect(b2.result.current.state.notice).toContain("恢复副本");
    // 原 head 纹丝不动；副本作为自己的分片提交；胶囊已删除。
    expect(headOf("alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(headOf(copyId)).toMatchObject({ revision: `1.${TAB_B}`, writerId: TAB_B });
    expect(window.localStorage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
    b2.unmount();

    // exactly once：再次对账（胶囊已删）⇒ 只有一份恢复副本，id 稳定。
    const rigB3: TabRig = {
      adapter: createConversationPersistenceAdapter({ identity: { writerSessionId: TAB_B, documentInstanceId: "doc-b-retry" } }),
      session: tabB.session,
      channel: bus.channelB,
    };
    const b3 = mountTab(rigB3);
    await act(async () => {
      await settle();
    });
    const copies = b3.result.current.state.conversations.filter((conversation) => conversation.id === copyId);
    expect(copies).toHaveLength(1);
    expect(b3.result.current.state.conversations.filter((conversation) => conversation.title.endsWith("（恢复副本）"))).toHaveLength(1);
    expect(localStorageKeys().filter((key) => key.startsWith(sessionSnapshotKeyV3(copyId, "")))).toHaveLength(1);
  });

  it("a tombstoned conversation comes back as a recovery copy without resurrecting the id", async () => {
    seedConversations([makeConversation("alpha", "初版"), makeConversation("beta", "二")]);
    // A 全程在线（先于 B 的胶囊挂载，启动对账不会提前回收它）。
    const a = mountTab(tabA);
    const b = mountTab(tabB);
    act(() => {
      b.result.current.renameConversation("alpha", "B 的未提交标题");
    });
    const setItem = installSetItemSpy((key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix));
    act(() => {
      b.result.current.flushConversationPersistence();
    });
    setItem.mockRestore();
    b.unmount();

    // 另一标签页删除 alpha：tombstone 先于 head 移除落盘。
    await act(async () => {
      a.result.current.deleteConversation("alpha");
      await settle();
    });
    expect(window.localStorage.getItem(sessionHeadKeyV3("alpha"))).toBeNull();

    // B 会话恢复（同 tabId，新适配器）：tombstone 覆盖 ⇒ 恢复副本路径。
    const rigB2: TabRig = {
      adapter: createConversationPersistenceAdapter({ identity: { writerSessionId: TAB_B, documentInstanceId: "doc-b-tombstone" } }),
      session: tabB.session,
      channel: bus.channelB,
    };
    const b2 = mountTab(rigB2);
    await act(async () => {
      await settle();
    });

    const copyId = recoveredCopyIdV3("alpha", TAB_B, 1);
    const copy = b2.result.current.state.conversations.find((conversation) => conversation.id === copyId);
    expect(copy).toBeTruthy();
    expect(copy?.title).toBe("B 的未提交标题（恢复副本）");
    expect(b2.result.current.state.notice).toContain("恢复副本");
    // tombstoned id 绝不复活：无 head、无快照，tombstone 原样保留。
    expect(window.localStorage.getItem(sessionHeadKeyV3("alpha"))).toBeNull();
    expect(window.localStorage.getItem(sessionTombstoneKeyV3("alpha"))).not.toBeNull();
    const alphaSnapshots = localStorageKeys().filter((key) => key.startsWith(sessionSnapshotKeyV3("alpha", "")));
    expect(alphaSnapshots).toHaveLength(1);
    expect(alphaSnapshots[0]?.startsWith(sessionSnapshotKeyV3(copyId, ""))).toBe(true);
    expect(window.localStorage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
  });

  it("records the capsule write failure into persistence health without throwing", () => {
    seedConversations([makeConversation("alpha", "初版")]);
    const a = mountTab(tabA);
    act(() => {
      a.result.current.renameConversation("alpha", "A 的未提交标题");
    });

    // 分片提交与恢复键都写失败：flush 与胶囊写入双双失败，两次失败都只进健康链路。
    const setItem = installSetItemSpy((key) =>
      key.startsWith(conversationStorageKeys.v3SnapshotPrefix) || key.startsWith(conversationStorageKeys.v3RecoveryPrefix));
    let flush: ReturnType<typeof a.result.current.flushConversationPersistence> | undefined;
    act(() => {
      flush = a.result.current.flushConversationPersistence();
    });
    setItem.mockRestore();

    expect(flush).toMatchObject({ ok: false });
    const health = getPersistenceHealthSnapshot();
    expect(health.healthy).toBe(false);
    expect(health.failedIds).toEqual(["conversation"]);
    expect(health.lastErrors.conversation?.message).toContain("恢复胶囊写入失败");
    expect(window.localStorage.getItem(sessionRecoveryKeyV3(TAB_A))).toBeNull();
  });
});
