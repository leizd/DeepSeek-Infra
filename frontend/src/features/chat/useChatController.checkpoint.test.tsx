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
  decideCheckpointDelay,
  EDIT_COALESCE_MS,
  STREAM_COMMIT_INTERVAL_MS,
} from "./checkpointSchedule";
import { resetPersistenceHealthForTests } from "../../app/persistenceHealth";
import { TAB_ID_STORAGE_KEY } from "../../app/tabIdentity";
import {
  conversationStorageKeys,
  createConversationPersistenceAdapter,
  sessionHeadKeyV3,
  sessionSnapshotKeyV3,
  type ConversationPersistenceAdapter,
  type StorageLike,
} from "../../domain/conversation/persistence";
import type { Conversation } from "../../domain/conversation/types";
import type { ChatMessage } from "../../domain/chat/types";
import type { ConversationSyncChannel } from "../../app/conversationSync";

const TAB = "cccc0003";

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

function stubChannel(): ConversationSyncChannel {
  return { post: () => undefined, subscribe: () => () => undefined };
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

interface Rig {
  adapter: ConversationPersistenceAdapter;
  session: MemorySession;
  channel: ConversationSyncChannel;
}

function makeRig(): Rig {
  return { adapter: createConversationPersistenceAdapter(), session: makeSession(TAB), channel: stubChannel() };
}

/** 缺省调度策略（300ms 合并 / 1s 节流 / 结算即时提交），fake timers 驱动。 */
function mount(rig: Rig) {
  return renderHook(() => useChatController({ persistence: rig.adapter, syncChannel: rig.channel, session: rig.session }));
}

function seed(rig: Rig, conversations: Conversation[]): void {
  rig.adapter.save(
    { schemaVersion: 1, currentConversationId: conversations[0]?.id ?? null, conversations },
    window.localStorage,
    rig.session,
  );
}

/** 手动推进的流：push 的事件按序被消费；消费尽后挂起等待下一次 push。 */
function controlledStream() {
  const queue: ChatStreamEvent[] = [];
  let notify: (() => void) | null = null;
  const stream = (async function* (): AsyncGenerator<ChatStreamEvent> {
    for (;;) {
      while (queue.length) {
        yield queue.shift() as ChatStreamEvent;
      }
      await new Promise<void>((resolve) => {
        notify = resolve;
      });
    }
  })();
  return {
    stream,
    push(event: ChatStreamEvent): void {
      queue.push(event);
      const wake = notify;
      notify = null;
      wake?.();
    },
  };
}

function failingStream(): AsyncGenerator<ChatStreamEvent> {
  return (async function* stream() {
    throw new Error("网络中断");
  })();
}

/**
 * 推进假时钟并排空微任务：定时器回调里的仲裁提交是同步发起、异步收尾
 * （单-flight running 标记在 .then 里复位），同步 act 之间必须给微任务
 * 一个 checkpoint，否则后续提交会被 flight 队列吞掉。
 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    await Promise.resolve();
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  streamChatMock.mockReset();
  settingsStub.apiKey = "sk-test";
  settingsStub.runtime = null;
  settingsStub.agentMode = false;
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  resetPersistenceHealthForTests();
});

describe("decideCheckpointDelay", () => {
  const base = { streamingActive: false, justSettled: false, lastCommitAt: 0, now: 10_000 };

  it("coalesces normal edits over the trailing window", () => {
    expect(decideCheckpointDelay(base)).toBe(EDIT_COALESCE_MS);
  });

  it("commits immediately on stream settle regardless of the streaming flag", () => {
    expect(decideCheckpointDelay({ ...base, justSettled: true })).toBe("immediate");
    expect(decideCheckpointDelay({ ...base, streamingActive: true, justSettled: true })).toBe("immediate");
  });

  it("throttles in-flight streaming to the remaining interval anchored at the last commit", () => {
    expect(decideCheckpointDelay({ ...base, streamingActive: true, lastCommitAt: 9_600 }))
      .toBe(STREAM_COMMIT_INTERVAL_MS - 400);
    // 锚定上次提交而非最近事件：重复调度收敛到同一尾随边界，绝不无限推后。
    expect(decideCheckpointDelay({ ...base, streamingActive: true, lastCommitAt: 9_600, now: 10_100 }))
      .toBe(STREAM_COMMIT_INTERVAL_MS - 500);
  });

  it("fires without extra delay once a full streaming interval has elapsed", () => {
    expect(decideCheckpointDelay({ ...base, streamingActive: true, lastCommitAt: 0 })).toBe(0);
    expect(decideCheckpointDelay({ ...base, streamingActive: true, lastCommitAt: 9_000 })).toBe(0);
  });

  it("never returns a negative delay for out-of-order clocks", () => {
    expect(decideCheckpointDelay({ ...base, streamingActive: true, lastCommitAt: 10_500 }))
      .toBe(STREAM_COMMIT_INTERVAL_MS);
  });
});

describe("useChatController checkpoint scheduling", () => {
  it("coalesces a burst of normal edits within the window into exactly one commit", async () => {
    const rig = makeRig();
    seed(rig, [makeConversation("alpha", "一")]);
    const saveArbitrated = vi.spyOn(rig.adapter, "saveArbitrated");
    const { result } = mount(rig);

    act(() => {
      result.current.renameConversation("alpha", "编辑一");
    });
    await advance(100);
    act(() => {
      result.current.renameConversation("alpha", "编辑二");
    });
    await advance(100);
    act(() => {
      result.current.renameConversation("alpha", "编辑三");
    });

    // 窗口内连续编辑（含挂载提交）被反复重置：窗口未满零提交。
    await advance(EDIT_COALESCE_MS - 1);
    expect(saveArbitrated).not.toHaveBeenCalled();
    await advance(1);
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
    await advance(2_000);
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
    // 合并后的提交落盘的是最终编辑。
    expect(rig.adapter.readSharedConversation("alpha")?.conversation.title).toBe("编辑三");
  });

  it("throttles in-flight streaming to one commit per second; the trailing edge captures the final delta", async () => {
    const rig = makeRig();
    seed(rig, [makeConversation("alpha", "一")]);
    const saveArbitrated = vi.spyOn(rig.adapter, "saveArbitrated");
    const { result } = mount(rig);
    // 挂载（no-op）提交落地，作为后续流式节流的锚点。
    await advance(EDIT_COALESCE_MS);
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
    saveArbitrated.mockClear();

    const gated = controlledStream();
    streamChatMock.mockImplementationOnce(() => gated.stream);
    act(() => {
      result.current.tryStartMessage("问题");
    });
    expect(result.current.state.requestStatus).toBe("streaming");

    const push = async (event: ChatStreamEvent): Promise<void> => {
      await act(async () => {
        gated.push(event);
      });
    };

    // 5 个内容增量跨越 3 秒：每秒至多一次提交（节流边界锚定上次提交时刻）。
    await push({ type: "content", text: "d1" });
    await advance(600);
    await push({ type: "content", text: "d2" });
    await advance(STREAM_COMMIT_INTERVAL_MS - 601);
    expect(saveArbitrated).not.toHaveBeenCalled();
    await advance(1);
    expect(saveArbitrated).toHaveBeenCalledTimes(1); // 节流提交 #1（距上次提交 1000ms）

    await push({ type: "content", text: "d3" });
    await advance(600);
    await push({ type: "content", text: "d4" });
    await advance(STREAM_COMMIT_INTERVAL_MS - 601);
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(saveArbitrated).toHaveBeenCalledTimes(2); // 节流提交 #2

    await push({ type: "content", text: "d5" });
    await advance(STREAM_COMMIT_INTERVAL_MS - 1);
    expect(saveArbitrated).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(saveArbitrated).toHaveBeenCalledTimes(3); // 尾随提交 #3：流仍在进行
    // 尾随边界不丢尾部增量：d5 已耐久落盘。
    expect(rig.adapter.readSharedConversation("alpha")?.conversation.messages.at(-1)?.content).toBe("d1d2d3d4d5");

    // 结算立即提交终态（绕过下一个节流边界）。
    await push({ type: "done" });
    expect(result.current.state.requestStatus).toBe("idle");
    expect(saveArbitrated).toHaveBeenCalledTimes(4);
    expect(rig.adapter.readSharedConversation("alpha")?.conversation.messages.at(-1)?.phase).toBe("done");
    await advance(5_000);
    expect(saveArbitrated).toHaveBeenCalledTimes(4);
  });

  it("commits immediately when the stream completes, ahead of the pending throttle boundary", async () => {
    const rig = makeRig();
    seed(rig, [makeConversation("alpha", "一")]);
    const saveArbitrated = vi.spyOn(rig.adapter, "saveArbitrated");
    const { result } = mount(rig);
    await advance(EDIT_COALESCE_MS);
    saveArbitrated.mockClear();

    const gated = controlledStream();
    streamChatMock.mockImplementationOnce(() => gated.stream);
    act(() => {
      result.current.tryStartMessage("问题");
    });
    await act(async () => {
      gated.push({ type: "content", text: "片段" });
    });
    // 距节流边界（1s）还远，且时钟完全未推进：结算必须立即提交。
    await act(async () => {
      gated.push({ type: "done" });
    });
    expect(result.current.state.requestStatus).toBe("idle");
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
    const committed = rig.adapter.readSharedConversation("alpha")?.conversation;
    expect(committed?.messages.at(-1)?.content).toContain("片段");
    expect(committed?.messages.at(-1)?.phase).toBe("done");
    // 待决的节流定时器已取消，无双提交。
    await advance(5_000);
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
  });

  it("commits immediately when the stream fails, ahead of the pending throttle boundary", async () => {
    const rig = makeRig();
    seed(rig, [makeConversation("alpha", "一")]);
    const saveArbitrated = vi.spyOn(rig.adapter, "saveArbitrated");
    const { result } = mount(rig);
    await advance(EDIT_COALESCE_MS);
    saveArbitrated.mockClear();

    streamChatMock.mockImplementationOnce(() => failingStream());
    // 先渲染出 streaming（streaming→idle 跃迁才可观测），再让失败传播结算。
    act(() => {
      result.current.tryStartMessage("会失败的问题");
    });
    expect(result.current.state.requestStatus).toBe("streaming");
    await advance(0);

    expect(result.current.state.requestStatus).toBe("idle");
    // requestFailed 同样是结算：立即提交，用户消息与错误态已耐久。
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
    const committed = rig.adapter.readSharedConversation("alpha")?.conversation;
    expect(committed?.messages.some((message) => message.role === "user" && message.content === "会失败的问题")).toBe(true);
    await advance(5_000);
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
  });

  it("commits immediately when the stream is interrupted, ahead of the pending throttle boundary", async () => {
    const rig = makeRig();
    seed(rig, [makeConversation("alpha", "一")]);
    const saveArbitrated = vi.spyOn(rig.adapter, "saveArbitrated");
    const { result } = mount(rig);
    await advance(EDIT_COALESCE_MS);
    saveArbitrated.mockClear();

    streamChatMock.mockImplementationOnce((_payload: unknown, options: { signal: AbortSignal }) =>
      (async function* (): AsyncGenerator<ChatStreamEvent> {
        yield { type: "content", text: "中断前片段" };
        await new Promise<never>((_resolve, reject) => {
          const fail = () => reject(new DOMException("已中止", "AbortError"));
          if (options.signal.aborted) {
            fail();
            return;
          }
          options.signal.addEventListener("abort", fail);
        });
      })(),
    );
    act(() => {
      result.current.tryStartMessage("问题");
    });
    expect(result.current.state.requestStatus).toBe("streaming");
    // 让消费者先进入 abort 等待（微任务 checkpoint）。
    await advance(0);

    await act(async () => {
      result.current.stopGeneration();
    });
    expect(result.current.state.requestStatus).toBe("idle");
    // requestStopped 同样是结算：立即提交，中断态已耐久（诚实恢复为可续写）。
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
    const committed = rig.adapter.readSharedConversation("alpha")?.conversation;
    expect(committed?.messages.at(-1)?.content).toContain("中断前片段");
    expect(committed?.messages.at(-1)?.interrupted).toBe(true);
    await advance(5_000);
    expect(saveArbitrated).toHaveBeenCalledTimes(1);
  });

  it("flushConversationPersistence (pagehide / build-update activation) commits pending dirty state now, cancels the timer, never double-commits", async () => {
    const rig = makeRig();
    seed(rig, [makeConversation("alpha", "一")]);
    const save = vi.spyOn(rig.adapter, "save");
    const saveArbitrated = vi.spyOn(rig.adapter, "saveArbitrated");
    const { result } = mount(rig);
    await advance(EDIT_COALESCE_MS);
    save.mockClear();
    saveArbitrated.mockClear();

    act(() => {
      result.current.renameConversation("alpha", "待 flush");
    });
    await advance(100);
    // 防抖定时器仍待决：生命周期 flush 立即同步提交并取消它。
    let flush: ReturnType<typeof result.current.flushConversationPersistence> | undefined;
    act(() => {
      flush = result.current.flushConversationPersistence();
    });
    expect(flush).toMatchObject({ ok: true });
    expect(save).toHaveBeenCalledTimes(1);
    expect(rig.adapter.readSharedConversation("alpha")?.conversation.title).toBe("待 flush");

    await advance(5_000);
    expect(saveArbitrated).not.toHaveBeenCalled();
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("serializes only the dirty conversation shard; GC removes at most two stale snapshots per commit", async () => {
    const rig = makeRig();
    seed(rig, [makeConversation("alpha", "一"), makeConversation("beta", "二")]);
    const { result } = mount(rig);
    await advance(EDIT_COALESCE_MS);

    const writes: string[] = [];
    const removals: string[] = [];
    const originalSet = Object.getOwnPropertyDescriptor(Storage.prototype, "setItem")?.value as (
      this: Storage,
      key: string,
      value: string,
    ) => void;
    const originalRemove = Object.getOwnPropertyDescriptor(Storage.prototype, "removeItem")?.value as (
      this: Storage,
      key: string,
    ) => void;
    const setSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key: string, value: string) {
      if (this === window.localStorage) writes.push(key);
      originalSet.call(this, key, value);
    });
    const removeSpy = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(function (this: Storage, key: string) {
      if (this === window.localStorage) removals.push(key);
      originalRemove.call(this, key);
    });

    // 只改 alpha：提交只序列化 alpha 分片，beta 的 snapshot / head 纹丝不动。
    act(() => {
      result.current.renameConversation("alpha", "只改 alpha");
    });
    await advance(EDIT_COALESCE_MS);
    const snapshotWrites = writes.filter((key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix));
    expect(snapshotWrites).toHaveLength(1);
    expect(snapshotWrites[0]?.startsWith(sessionSnapshotKeyV3("alpha", ""))).toBe(true);
    expect(writes.some((key) => key.startsWith(sessionSnapshotKeyV3("beta", "")))).toBe(false);
    expect(writes.some((key) => key.startsWith(sessionHeadKeyV3("beta")))).toBe(false);

    // 第二次提交：旧 revision 由有界保留 GC 回收（≤2 键/次），且仍是 alpha 自己的分片。
    writes.length = 0;
    removals.length = 0;
    act(() => {
      result.current.renameConversation("alpha", "再改 alpha");
    });
    await advance(EDIT_COALESCE_MS);
    const secondSnapshotWrites = writes.filter((key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix));
    expect(secondSnapshotWrites).toHaveLength(1);
    expect(secondSnapshotWrites[0]?.startsWith(sessionSnapshotKeyV3("alpha", ""))).toBe(true);
    expect(writes.some((key) => key.startsWith(sessionSnapshotKeyV3("beta", "")))).toBe(false);
    const snapshotRemovals = removals.filter((key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix));
    expect(snapshotRemovals.length).toBeLessThanOrEqual(2);
    expect(snapshotRemovals.every((key) => key.startsWith(sessionSnapshotKeyV3("alpha", "")))).toBe(true);
    setSpy.mockRestore();
    removeSpy.mockRestore();
  });

  it("never fires a pending timer after unmount", async () => {
    const rig = makeRig();
    seed(rig, [makeConversation("alpha", "一")]);
    const saveArbitrated = vi.spyOn(rig.adapter, "saveArbitrated");
    const { result, unmount } = mount(rig);
    act(() => {
      result.current.renameConversation("alpha", "未提交");
    });
    unmount();
    await advance(10_000);
    expect(saveArbitrated).not.toHaveBeenCalled();
  });
});
