import { describe, expect, it, vi } from "vitest";

import { TAB_ID_STORAGE_KEY } from "../../app/tabIdentity";
import type { ChatMessage } from "../chat/types";
import type { Conversation } from "./types";
import {
  CONVERSATION_CHECKPOINT_LOCK_NAME,
  conversationStorageKeys,
  createConversationPersistenceAdapter,
  runIdleCheckpointGc,
  sessionConflictKeyV3,
  sessionHeadKeyV3,
  sessionSnapshotKeyV3,
  type ConversationCommitNotice,
  type ConversationConflictPointer,
  type ConversationConflictSignal,
  type LockRequestOptions,
  type LocksLike,
  type SaveConversationOptions,
  type StorageLike,
} from "./persistence";

const TAB_A = "aaaa0001";
const TAB_B = "bbbb0002";

class MemoryStorage implements StorageLike {
  readonly values = new Map<string, string>();
  failOnSet: ((key: string) => boolean) | null = null;
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) {
    if (this.failOnSet?.(key)) throw new Error("setItem failed");
    this.values.set(key, value);
  }
  removeItem(key: string) { this.values.delete(key); }
  get length(): number { return this.values.size; }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null; }
}

/** 公平互斥锁：按请求顺序串行执行回调，模拟 Web Locks 的排他模式。 */
class FakeMutex implements LocksLike {
  private tail: Promise<unknown> = Promise.resolve();
  readonly names: string[] = [];
  request<T>(name: string, _options: LockRequestOptions, callback: () => T | Promise<T>): Promise<T> {
    this.names.push(name);
    const run = this.tail.then(callback);
    this.tail = run.catch(() => undefined);
    return run;
  }
}

function makeSession(tabId: string): MemoryStorage {
  const session = new MemoryStorage();
  session.setItem(TAB_ID_STORAGE_KEY, tabId);
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

function editConversation(conversation: Conversation, content: string): Conversation {
  return {
    ...conversation,
    messages: [...conversation.messages, makeMessage(`${conversation.id}-edit-${content}`, content)],
    updatedAt: conversation.updatedAt + 1,
  };
}

function makeState(currentConversationId: string | null, conversations: Conversation[]) {
  return { schemaVersion: 1 as const, currentConversationId, conversations };
}

function readHead(storage: MemoryStorage, conversationId: string): Record<string, unknown> {
  return JSON.parse(storage.getItem(sessionHeadKeyV3(conversationId)) ?? "{}") as Record<string, unknown>;
}

function readPointer(storage: MemoryStorage, conversationId: string): ConversationConflictPointer | null {
  const raw = storage.getItem(sessionConflictKeyV3(conversationId));
  return raw ? (JSON.parse(raw) as ConversationConflictPointer) : null;
}

function snapshotKeys(storage: MemoryStorage, conversationId: string): string[] {
  return [...storage.values.keys()].filter((key) => key.startsWith(sessionSnapshotKeyV3(conversationId, "")));
}

interface Recorder {
  commits: ConversationCommitNotice[];
  conflicts: ConversationConflictSignal[];
  options: SaveConversationOptions;
}

function makeRecorder(): Recorder {
  const recorder: Recorder = {
    commits: [],
    conflicts: [],
    options: {},
  };
  recorder.options = {
    onCommit: (notice) => recorder.commits.push(notice),
    onConflict: (signal) => recorder.conflicts.push(signal),
  };
  return recorder;
}

describe("cross-tab checkpoint arbitration", () => {
  it("two tabs editing DIFFERENT conversations concurrently both advance their heads", async () => {
    const storage = new MemoryStorage();
    const mutex = new FakeMutex();
    const adapterA = createConversationPersistenceAdapter({ locks: mutex });
    const adapterB = createConversationPersistenceAdapter({ locks: mutex });
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    const alpha0 = makeConversation("alpha", "一");
    const beta0 = makeConversation("beta", "二");
    adapterA.save(makeState("alpha", [alpha0, beta0]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);

    // A 用 A 自己的对象（beta 对 A 干净）；B 用 B 加载到的对象（alpha 对 B 干净）。
    const editedAlpha = editConversation(alpha0, "A 的修改");
    const editedBeta = editConversation(loadedB.conversations.find((c) => c.id === "beta") as Conversation, "B 的修改");
    const stateA = makeState("alpha", [editedAlpha, beta0]);
    const stateB = makeState("beta", [loadedB.conversations.find((c) => c.id === "alpha") as Conversation, editedBeta]);

    const recorderA = makeRecorder();
    const recorderB = makeRecorder();
    const [resultA, resultB] = await Promise.all([
      adapterA.saveArbitrated(() => stateA, storage, sessionA, recorderA.options),
      adapterB.saveArbitrated(() => stateB, storage, sessionB, recorderB.options),
    ]);

    expect(resultA).toMatchObject({ ok: true });
    expect(resultB).toMatchObject({ ok: true });
    expect(mutex.names).toEqual([CONVERSATION_CHECKPOINT_LOCK_NAME, CONVERSATION_CHECKPOINT_LOCK_NAME]);
    // 两个会话的 head 分别由两个标签页推进，互不干扰，也没有冲突指针。
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(readHead(storage, "beta")).toMatchObject({ revision: `2.${TAB_B}`, writerId: TAB_B });
    expect(storage.getItem(sessionConflictKeyV3("alpha"))).toBeNull();
    expect(storage.getItem(sessionConflictKeyV3("beta"))).toBeNull();
    expect(recorderA.conflicts).toEqual([]);
    expect(recorderB.conflicts).toEqual([]);
    // 双方内容都完整保留。
    const reloaded = adapterA.load(storage, sessionA);
    const contents = Object.fromEntries(reloaded.conversations.map((c) => [c.id, c.messages.at(-1)?.content]));
    expect(contents).toEqual({ alpha: "A 的修改", beta: "B 的修改" });
  });

  it("two tabs editing the SAME conversation concurrently: loser branches, winner head intact", async () => {
    const storage = new MemoryStorage();
    const mutex = new FakeMutex();
    const adapterA = createConversationPersistenceAdapter({ locks: mutex });
    const adapterB = createConversationPersistenceAdapter({ locks: mutex });
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    const base = loadedB.conversations[0] as Conversation;

    const recorderA = makeRecorder();
    const recorderB = makeRecorder();
    const stateA = makeState("alpha", [editConversation(base, "A 的修改")]);
    const stateB = makeState("alpha", [editConversation(base, "B 的修改")]);
    const [resultA, resultB] = await Promise.all([
      adapterA.saveArbitrated(() => stateA, storage, sessionA, recorderA.options),
      adapterB.saveArbitrated(() => stateB, storage, sessionB, recorderB.options),
    ]);

    // 胜方推进 head；负方分支保存为冲突副本，head 绝不被覆盖。
    expect(resultA).toMatchObject({ ok: true });
    expect(resultB).toMatchObject({ ok: true });
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}`, parentRevision: `1.${TAB_A}`, writerId: TAB_A });

    const pointer = readPointer(storage, "alpha");
    expect(pointer).toMatchObject({
      revision: `3.${TAB_B}`,
      baseRevision: `1.${TAB_A}`,
      sharedRevision: `2.${TAB_A}`,
      writerId: TAB_B,
    });
    expect(typeof pointer?.savedAt).toBe("number");

    // 冲突分支快照包含负方内容；胜方快照未被触碰。
    const branch = adapterB.readConflictBranch("alpha", storage);
    expect(branch?.conversation.messages.at(-1)?.content).toBe("B 的修改");
    expect(branch?.pointer).toEqual(pointer);
    const shared = adapterB.readSharedConversation("alpha", storage);
    expect(shared?.revision).toBe(`2.${TAB_A}`);
    expect(shared?.conversation.messages.at(-1)?.content).toBe("A 的修改");

    // 冲突信号带外上报；两次成功提交都产生 commit 通知。
    expect(recorderB.conflicts).toHaveLength(1);
    expect(recorderB.conflicts[0]).toMatchObject({
      conversationId: "alpha",
      revision: `3.${TAB_B}`,
      baseRevision: `1.${TAB_A}`,
      sharedRevision: `2.${TAB_A}`,
      writerId: TAB_B,
    });
    expect(recorderA.conflicts).toEqual([]);
    expect(recorderA.commits).toEqual([expect.objectContaining({ conversationId: "alpha", revision: `2.${TAB_A}`, writerId: TAB_A })]);
    expect(recorderB.commits).toEqual([expect.objectContaining({ conversationId: "alpha", revision: `3.${TAB_B}`, writerId: TAB_B })]);

    // 负方 base 已跟进胜方：继续编辑的下一次提交在胜方之上正常推进。
    const stateB2 = makeState("alpha", [editConversation(stateB.conversations[0] as Conversation, "B 的后续")]);
    const followUp = adapterB.save(stateB2, storage, sessionB, recorderB.options);
    expect(followUp).toMatchObject({ ok: true });
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `3.${TAB_B}`, parentRevision: `2.${TAB_A}`, writerId: TAB_B });
  });

  it("a lock-free writer with a stale base cannot advance head (sibling detection without Web Locks)", () => {
    const storage = new MemoryStorage();
    const adapterA = createConversationPersistenceAdapter();
    // locks: null 强制无锁路径——与走锁路径完全相同的重读 + 比较。
    const adapterB = createConversationPersistenceAdapter({ locks: null });
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    const stateA = makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "A 的修改")]);
    const stateB = makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "B 的修改")]);

    expect(adapterA.save(stateA, storage, sessionA)).toMatchObject({ ok: true });
    const resultB = adapterB.save(stateB, storage, sessionB);

    // 无锁并发提交退化为冲突分支，绝不是静默 last-write-wins。
    expect(resultB).toMatchObject({ ok: true });
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(readPointer(storage, "alpha")).toMatchObject({ revision: `3.${TAB_B}`, sharedRevision: `2.${TAB_A}`, writerId: TAB_B });
    expect(adapterB.readConflictBranch("alpha", storage)?.conversation.messages.at(-1)?.content).toBe("B 的修改");
  });

  it("saveArbitrated without any lock implementation (no Web Locks environment) keeps conflict safety", async () => {
    const storage = new MemoryStorage();
    // 不注入锁，node 环境没有 navigator.locks → 自动退化无锁路径。
    const adapterA = createConversationPersistenceAdapter();
    const adapterB = createConversationPersistenceAdapter();
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    const edited = editConversation(loadedB.conversations[0] as Conversation, "B 的修改");
    adapterA.save(makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "A 的修改")]), storage, sessionA);

    const result = await adapterB.saveArbitrated(() => makeState("alpha", [edited]), storage, sessionB);

    expect(result).toMatchObject({ ok: true });
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(readPointer(storage, "alpha")).toMatchObject({ revision: `3.${TAB_B}`, writerId: TAB_B });
  });

  it("a freshly loaded tab writes nothing and creates no spurious conflict", () => {
    const storage = new MemoryStorage();
    const adapterA = createConversationPersistenceAdapter();
    const adapterB = createConversationPersistenceAdapter();
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);

    const setSpy = vi.spyOn(storage, "setItem");
    const recorder = makeRecorder();
    const result = adapterB.save(makeState(loadedB.currentConversationId, [...loadedB.conversations]), storage, sessionB, recorder.options);

    expect(result).toMatchObject({ ok: true });
    expect(setSpy).not.toHaveBeenCalled();
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `1.${TAB_A}` });
    expect(recorder.conflicts).toEqual([]);
    expect(recorder.commits).toEqual([]);
  });

  it("a second conflict replaces the pointer only after the new branch is confirmed", () => {
    const storage = new MemoryStorage();
    const adapterA = createConversationPersistenceAdapter();
    const adapterB = createConversationPersistenceAdapter();
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    const base = loadedB.conversations[0] as Conversation;
    adapterA.save(makeState("alpha", [editConversation(base, "A 第一改")]), storage, sessionA);
    adapterB.save(makeState("alpha", [editConversation(base, "B 第一改")]), storage, sessionB);
    expect(readPointer(storage, "alpha")).toMatchObject({ revision: `3.${TAB_B}`, sharedRevision: `2.${TAB_A}` });

    // 胜方再次推进，负方（base 已跟进 2.tabA）再次落后 → 第二次冲突。
    const head2 = adapterA.readSharedConversation("alpha", storage)?.conversation as Conversation;
    adapterA.save(makeState("alpha", [editConversation(head2, "A 第二改")]), storage, sessionA);
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `3.${TAB_A}` });
    const staleBranch = adapterB.readConflictBranch("alpha", storage)?.conversation as Conversation;
    adapterB.save(makeState("alpha", [editConversation(staleBranch, "B 第二改")]), storage, sessionB);

    // 至多一个未解决冲突：指针替换为新的分支，旧分支失去保护。
    expect(readPointer(storage, "alpha")).toMatchObject({ revision: `4.${TAB_B}`, baseRevision: `2.${TAB_A}`, sharedRevision: `3.${TAB_A}` });
    expect(adapterB.readConflictBranch("alpha", storage)?.conversation.messages.at(-1)?.content).toBe("B 第二改");
  });

  it("conflict-branch write failure returns write-conflict and leaves head + pointer untouched", () => {
    const storage = new MemoryStorage();
    const adapterA = createConversationPersistenceAdapter();
    const adapterB = createConversationPersistenceAdapter();
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    adapterA.save(makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "A 的修改")]), storage, sessionA);

    storage.failOnSet = (key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix);
    const result = adapterB.save(
      makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "B 的修改")]),
      storage,
      sessionB,
    );

    expect(result).toMatchObject({ ok: false, code: "write-conflict" });
    if (result.ok) return;
    expect(result.message).toContain("会话 alpha");
    expect(result.message).toMatch(/\(~\d+ bytes\)$/);
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(storage.getItem(sessionConflictKeyV3("alpha"))).toBeNull();
  });

  it("conflict-pointer write failure after a verified branch reports write-conflict", () => {
    const storage = new MemoryStorage();
    const adapterA = createConversationPersistenceAdapter();
    const adapterB = createConversationPersistenceAdapter();
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    adapterA.save(makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "A 的修改")]), storage, sessionA);

    storage.failOnSet = (key) => key.startsWith(conversationStorageKeys.v3ConflictPrefix);
    const result = adapterB.save(
      makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "B 的修改")]),
      storage,
      sessionB,
    );

    expect(result).toMatchObject({ ok: false, code: "write-conflict" });
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    // 分支快照已核验落盘但无指针引用 → 成为孤儿，由空闲 GC 回收。
    expect(snapshotKeys(storage, "alpha")).toContain(sessionSnapshotKeyV3("alpha", `3.${TAB_B}`));
    expect(runIdleCheckpointGc(storage)).toBeGreaterThanOrEqual(1);
    expect(snapshotKeys(storage, "alpha")).not.toContain(sessionSnapshotKeyV3("alpha", `3.${TAB_B}`));
  });

  it("conflict-pointed snapshots survive retention and idle GC until resolved", () => {
    const storage = new MemoryStorage();
    const adapterA = createConversationPersistenceAdapter();
    const adapterB = createConversationPersistenceAdapter();
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    const base = loadedB.conversations[0] as Conversation;
    adapterA.save(makeState("alpha", [editConversation(base, "A 的修改")]), storage, sessionA);
    adapterB.save(makeState("alpha", [editConversation(base, "B 的修改")]), storage, sessionB);
    const branchKey = sessionSnapshotKeyV3("alpha", `3.${TAB_B}`);
    expect(storage.values.has(branchKey)).toBe(true);

    // 胜方继续提交（保留 GC）与空闲 GC 都不能回收冲突指针引用的分支。
    let head = adapterA.readSharedConversation("alpha", storage)?.conversation as Conversation;
    for (const content of ["A 二改", "A 三改", "A 四改"]) {
      head = editConversation(head, content);
      adapterA.save(makeState("alpha", [head]), storage, sessionA);
    }
    expect(storage.values.has(branchKey)).toBe(true);
    expect(runIdleCheckpointGc(storage, 8)).toBe(0);
    expect(storage.values.has(branchKey)).toBe(true);

    // 解决（清除指针）后，下一轮 GC 在预算内回收分支。
    adapterB.clearConflict("alpha", storage);
    expect(runIdleCheckpointGc(storage, 8)).toBe(1);
    expect(storage.values.has(branchKey)).toBe(false);
    expect(readPointer(storage, "alpha")).toBeNull();
  });

  it("readConflictBranch returns null when the branch snapshot is corrupt", () => {
    const storage = new MemoryStorage();
    const adapterA = createConversationPersistenceAdapter();
    const adapterB = createConversationPersistenceAdapter();
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    adapterA.save(makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "A 的修改")]), storage, sessionA);
    adapterB.save(makeState("alpha", [editConversation(loadedB.conversations[0] as Conversation, "B 的修改")]), storage, sessionB);
    storage.setItem(sessionSnapshotKeyV3("alpha", `3.${TAB_B}`), "garbage{{{");

    expect(adapterB.readConflictBranch("alpha", storage)).toBeNull();
  });

  it("reconcileRemoteCommit reloads a clean local copy and marks a dirty one stale", () => {
    const storage = new MemoryStorage();
    const adapterA = createConversationPersistenceAdapter();
    const adapterB = createConversationPersistenceAdapter();
    const sessionA = makeSession(TAB_A);
    const sessionB = makeSession(TAB_B);

    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版"), makeConversation("beta", "二")]), storage, sessionA);
    const loadedB = adapterB.load(storage, sessionB);
    const localAlpha = loadedB.conversations.find((c) => c.id === "alpha") as Conversation;
    const localBeta = loadedB.conversations.find((c) => c.id === "beta") as Conversation;

    // 远端同时推进 alpha 与 beta；B 对 alpha 干净 → reload；B 对 beta 有未提交修改 → stale。
    adapterA.save(
      makeState("alpha", [editConversation(localAlpha, "A 改 alpha"), editConversation(localBeta, "A 改 beta")]),
      storage,
      sessionA,
    );
    const dirtyBeta = editConversation(localBeta, "B 的修改");

    const clean = adapterB.reconcileRemoteCommit("alpha", localAlpha, storage);
    expect(clean.kind).toBe("reload");
    if (clean.kind === "reload") expect(clean.conversation.messages.at(-1)?.content).toBe("A 改 alpha");

    const dirty = adapterB.reconcileRemoteCommit("beta", dirtyBeta, storage);
    expect(dirty.kind).toBe("stale");

    // 换入后的副本已登记为已提交：下一次 flush 不会重写它（只有 beta 的冲突写入）。
    if (clean.kind === "reload") {
      const setSpy = vi.spyOn(storage, "setItem");
      adapterB.save(makeState("alpha", [clean.conversation, dirtyBeta]), storage, sessionB);
      const alphaWrites = setSpy.mock.calls.filter(([key]) => key.includes("alpha"));
      expect(alphaWrites).toEqual([]);
    }

    // beta 的提交走冲突路径：head 不被覆盖，冲突分支落地。
    expect(readHead(storage, "beta")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(readPointer(storage, "beta")).toMatchObject({ revision: `3.${TAB_B}`, sharedRevision: `2.${TAB_A}` });
    expect(adapterB.readConflictBranch("beta", storage)?.conversation.messages.at(-1)?.content).toBe("B 的修改");
  });
});
