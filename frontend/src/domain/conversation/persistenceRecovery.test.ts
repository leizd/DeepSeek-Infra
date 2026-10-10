import { describe, expect, it } from "vitest";

import { TAB_ID_STORAGE_KEY } from "../../app/tabIdentity";
import type { ChatMessage } from "../chat/types";
import type { Conversation } from "./types";
import {
  CONVERSATION_TOMBSTONE_LIMITS,
  conversationStorageKeys,
  createConversationPersistenceAdapter,
  parseRecoveryCapsule,
  recoveredCopyIdV3,
  sessionHeadKeyV3,
  sessionRecoveryKeyV3,
  sessionSnapshotKeyV3,
  sessionTombstoneKeyV3,
  type ConversationCommitNotice,
  type ConversationConflictSignal,
  type ConversationDeleteNotice,
  type ConversationRecoverySignal,
  type SaveConversationOptions,
  type StorageLike,
} from "./persistence";

const TAB_A = "aaaa0001";
const TAB_B = "bbbb0002";
const TAB_C = "cccc0003";

function adapterFor(writerSessionId: string, documentInstanceId = `document-${writerSessionId}`) {
  return createConversationPersistenceAdapter({ identity: { writerSessionId, documentInstanceId } });
}

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

function snapshotKeys(storage: MemoryStorage, conversationId: string): string[] {
  return [...storage.values.keys()].filter((key) => key.startsWith(sessionSnapshotKeyV3(conversationId, "")));
}

function seedLease(storage: MemoryStorage, tabId: string, lastSeen: number): void {
  storage.setItem(`${conversationStorageKeys.v3TabPrefix}${tabId}`, JSON.stringify({ tabId, firstSeen: lastSeen, lastSeen }));
}

interface Recorder {
  commits: ConversationCommitNotice[];
  conflicts: ConversationConflictSignal[];
  deletes: ConversationDeleteNotice[];
  recoveries: ConversationRecoverySignal[];
  options: SaveConversationOptions;
}

function makeRecorder(): Recorder {
  const recorder: Recorder = { commits: [], conflicts: [], deletes: [], recoveries: [], options: {} };
  recorder.options = {
    onCommit: (notice) => recorder.commits.push(notice),
    onConflict: (signal) => recorder.conflicts.push(signal),
    onDelete: (notice) => recorder.deletes.push(notice),
    onRecovery: (signal) => recorder.recoveries.push(signal),
  };
  return recorder;
}

/** 让 adapter 对 conversation 形成带 base 的脏状态：先提交，再返回编辑后的脏对象。 */
function makeDirty(
  adapter: ReturnType<typeof createConversationPersistenceAdapter>,
  storage: MemoryStorage,
  session: MemoryStorage,
  conversation: Conversation,
  content: string,
): Conversation {
  adapter.save(makeState(conversation.id, [conversation]), storage, session);
  return editConversation(conversation, content);
}

describe("recovery capsule write + freshness", () => {
  it("a failed pagehide flush leaves a capsule with the dirty conversations and their per-cid bases", () => {
    const storage = new MemoryStorage();
    const adapter = adapterFor(TAB_A);
    const session = makeSession(TAB_A);
    const alpha = makeConversation("alpha", "初版");
    adapter.save(makeState("alpha", [alpha]), storage, session);
    const dirtyAlpha = editConversation(alpha, "未提交修改");
    const fresh = makeConversation("gamma", "新会话"); // 从未提交：base 为 null

    // 分片键写不进去 ⇒ flush 失败；恢复键可用 ⇒ 胶囊落盘。
    storage.failOnSet = (key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix);
    const flush = adapter.save(makeState("alpha", [dirtyAlpha, fresh]), storage, session);
    expect(flush.ok).toBe(false);
    storage.failOnSet = null;
    const failure = adapter.writeRecoveryCapsule(makeState("alpha", [dirtyAlpha, fresh]), storage, session);
    expect(failure).toBeNull();

    const capsule = parseRecoveryCapsule(storage.getItem(sessionRecoveryKeyV3(TAB_A)));
    expect(capsule).toMatchObject({ schemaVersion: 2, writerSessionId: TAB_A, sequence: 1 });
    expect(Object.fromEntries(capsule?.entries.map((entry) => [entry.conversationId, entry.baseRevision]) ?? []))
      .toEqual({ alpha: `1.${TAB_A}`, gamma: null });
    expect(capsule?.entries.map((entry) => entry.conversationId)).toEqual(["alpha", "gamma"]);
    expect(capsule?.savedAt).toBeGreaterThan(0);
    // 胶囊绝不推进任何共享 head。
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `1.${TAB_A}` });
    expect(storage.getItem(sessionHeadKeyV3("gamma"))).toBeNull();
  });

  it("a single dirty conversation is recorded with baseRevision only", () => {
    const storage = new MemoryStorage();
    const adapter = adapterFor(TAB_A);
    const session = makeSession(TAB_A);
    const dirty = makeDirty(adapter, storage, session, makeConversation("alpha", "初版"), "脏");

    expect(adapter.writeRecoveryCapsule(makeState("alpha", [dirty]), storage, session)).toBeNull();

    const capsule = parseRecoveryCapsule(storage.getItem(sessionRecoveryKeyV3(TAB_A)));
    expect(capsule?.entries[0]?.baseRevision).toBe(`1.${TAB_A}`);
    expect(capsule?.entries).toHaveLength(1);
  });

  it("a fully successful flush leaves no capsule key, and removes a stale one", () => {
    const storage = new MemoryStorage();
    const adapter = adapterFor(TAB_A);
    const session = makeSession(TAB_A);
    const alpha = makeConversation("alpha", "初版");
    adapter.save(makeState("alpha", [alpha]), storage, session);
    // 过期胶囊残留（此前某次页面退出写下，但此刻一切已提交）。
    storage.setItem(sessionRecoveryKeyV3(TAB_A), "stale");

    const failure = adapter.writeRecoveryCapsule(makeState("alpha", [alpha]), storage, session);

    expect(failure).toBeNull();
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_A))).toBeNull();
  });

  it("a normal successful commit that drains the dirty set removes this tab's capsule", () => {
    const storage = new MemoryStorage();
    const adapter = adapterFor(TAB_A);
    const session = makeSession(TAB_A);
    const dirty = makeDirty(adapter, storage, session, makeConversation("alpha", "初版"), "待提交");
    // 页面退出写过胶囊（BFCache 恢复后胶囊仍在）。
    expect(adapter.writeRecoveryCapsule(makeState("alpha", [dirty]), storage, session)).toBeNull();
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_A))).not.toBeNull();

    // 正常的防抖/仲裁提交落地 ⇒ 胶囊被移除（保鲜规则）。
    const result = adapter.save(makeState("alpha", [dirty]), storage, session);

    expect(result).toMatchObject({ ok: true });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_A))).toBeNull();
  });

  it("a partially successful commit rewrites the capsule with the remainder", () => {
    const storage = new MemoryStorage();
    const adapter = adapterFor(TAB_A);
    const session = makeSession(TAB_A);
    const alpha = makeConversation("alpha", "一");
    const beta = makeConversation("beta", "二");
    adapter.save(makeState("alpha", [alpha, beta]), storage, session);
    const dirtyAlpha = editConversation(alpha, "A 脏");
    const dirtyBeta = editConversation(beta, "B 脏");
    expect(adapter.writeRecoveryCapsule(makeState("alpha", [dirtyAlpha, dirtyBeta]), storage, session)).toBeNull();

    // 只有 beta 的分片写失败：alpha 提交成功，胶囊以剩余脏（beta）重写。
    storage.failOnSet = (key) => key.startsWith(sessionSnapshotKeyV3("beta", ""));
    const result = adapter.save(makeState("alpha", [dirtyAlpha, dirtyBeta]), storage, session);
    expect(result.ok).toBe(false);
    storage.failOnSet = null;

    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}` });
    const capsule = parseRecoveryCapsule(storage.getItem(sessionRecoveryKeyV3(TAB_A)));
    expect(capsule?.entries.map((entry) => entry.conversationId)).toEqual(["beta"]);
    expect(capsule?.entries[0]?.baseRevision).toBe(`1.${TAB_A}`);
  });

  it("a mid-session save failure does not create a capsule (page-exit only)", () => {
    const storage = new MemoryStorage();
    const adapter = adapterFor(TAB_A);
    const session = makeSession(TAB_A);
    const dirty = makeDirty(adapter, storage, session, makeConversation("alpha", "初版"), "脏");

    storage.failOnSet = (key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix);
    const result = adapter.save(makeState("alpha", [dirty]), storage, session);
    expect(result.ok).toBe(false);

    // 进行中的保存失败走健康/横幅链路；没有既有胶囊时不新建。
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_A))).toBeNull();
  });

  it("returns a classified failure instead of throwing when the capsule write itself fails", () => {
    const storage = new MemoryStorage();
    const adapter = adapterFor(TAB_A);
    const session = makeSession(TAB_A);
    const dirty = makeDirty(adapter, storage, session, makeConversation("alpha", "初版"), "脏");

    storage.failOnSet = (key) => key.startsWith(conversationStorageKeys.v3RecoveryPrefix);
    let failure: unknown;
    expect(() => {
      failure = adapter.writeRecoveryCapsule(makeState("alpha", [dirty]), storage, session);
    }).not.toThrow();

    expect(failure).toMatchObject({ ok: false, code: "unknown" });
    expect((failure as { message: string }).message).toContain("恢复胶囊写入失败");
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_A))).toBeNull();
  });
});

describe("startup capsule reconcile", () => {
  it("reconciles a clean capsule in place: head advances, content identical, capsule deleted", async () => {
    const storage = new MemoryStorage();
    const writer = adapterFor(TAB_B, "writer-b");
    const sessionB = makeSession(TAB_B);
    const dirty = makeDirty(writer, storage, sessionB, makeConversation("alpha", "初版"), "崩溃前的修改");
    expect(writer.writeRecoveryCapsule(makeState("alpha", [dirty]), storage, sessionB)).toBeNull();
    // 本标签页胶囊无租约条件：即便租约活跃（BFCache 场景）也照常对账。
    seedLease(storage, TAB_B, Date.now());

    // 会话恢复：同 tabId 的新适配器（浏览器 session restore 后的重启）。
    const reloaded = adapterFor(TAB_B, "reloaded-b");
    const recorder = makeRecorder();
    const outcome = await reloaded.reconcileRecoveryCapsules(storage, sessionB, recorder.options);

    expect(outcome.failed).toEqual([]);
    expect(outcome.recovered).toEqual([]);
    expect(outcome.committed).toHaveLength(1);
    expect(outcome.committed[0]).toMatchObject({ conversationId: "alpha", revision: `2.${TAB_B}` });
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_B}`, writerId: TAB_B });
    const loaded = reloaded.load(storage, sessionB);
    const committed = loaded.conversations.find((conversation) => conversation.id === "alpha");
    expect(committed?.messages.map((message) => message.content)).toEqual(dirty.messages.map((message) => message.content));
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
    expect(recorder.commits).toEqual([expect.objectContaining({ conversationId: "alpha", revision: `2.${TAB_B}`, writerId: TAB_B })]);
    expect(recorder.conflicts).toEqual([]);
  });

  it("commits a never-before-committed capsule conversation as its first shard", async () => {
    const storage = new MemoryStorage();
    const writer = adapterFor(TAB_B, "writer-b");
    const sessionB = makeSession(TAB_B);
    const fresh = makeConversation("gamma", "只存在于胶囊里");
    expect(writer.writeRecoveryCapsule(makeState("gamma", [fresh]), storage, sessionB)).toBeNull();

    const reloaded = adapterFor(TAB_B, "reloaded-b");
    const outcome = await reloaded.reconcileRecoveryCapsules(storage, sessionB);

    expect(outcome.committed).toHaveLength(1);
    expect(outcome.committed[0]).toMatchObject({ conversationId: "gamma", revision: `1.${TAB_B}` });
    expect(readHead(storage, "gamma")).toMatchObject({ revision: `1.${TAB_B}`, writerId: TAB_B });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
  });

  it("a moved head turns the capsule into a deterministic recovery copy, converging on reprocess", async () => {
    const storage = new MemoryStorage();
    const adapterA = adapterFor(TAB_A);
    const sessionA = makeSession(TAB_A);
    const writer = adapterFor(TAB_B, "writer-b");
    const sessionB = makeSession(TAB_B);
    const alpha0 = makeConversation("alpha", "初版");
    adapterA.save(makeState("alpha", [alpha0]), storage, sessionA);
    const loadedB = writer.load(storage, sessionB);
    const dirty = editConversation(loadedB.conversations[0] as Conversation, "B 的未提交修改");
    expect(writer.writeRecoveryCapsule(makeState("alpha", [dirty]), storage, sessionB)).toBeNull();
    const capsuleRaw = storage.getItem(sessionRecoveryKeyV3(TAB_B)) as string;
    // 兄弟标签页推进共享 head：胶囊 base 已过期。
    adapterA.save(makeState("alpha", [editConversation(alpha0, "A 的修改")]), storage, sessionA);
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}` });

    // 崩溃的 B 会话恢复（同 tabId 新适配器）：物化确定性恢复副本。
    const reloadedB = adapterFor(TAB_B, "reloaded-b");
    const outcome = await reloadedB.reconcileRecoveryCapsules(storage, sessionB);

    const copyId = recoveredCopyIdV3("alpha", TAB_B, 1);
    expect(outcome.failed).toEqual([]);
    expect(outcome.committed).toEqual([]);
    expect(outcome.recovered).toHaveLength(1);
    expect(outcome.recovered[0]).toMatchObject({ id: copyId, title: "会话 alpha（恢复副本）" });
    expect(outcome.recovered[0]?.messages.at(-1)?.content).toBe("B 的未提交修改");
    // 原 head 纹丝不动；副本作为自己的分片提交；胶囊已删除。
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_A}`, writerId: TAB_A });
    expect(readHead(storage, copyId)).toMatchObject({ revision: `1.${TAB_B}`, writerId: TAB_B });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
    expect(snapshotKeys(storage, copyId)).toHaveLength(1);

    // 同一胶囊重处理（模拟副本已提交但胶囊删除前属主再次死亡）：收敛，绝不重复。
    storage.setItem(sessionRecoveryKeyV3(TAB_B), capsuleRaw);
    storage.removeItem(`${conversationStorageKeys.v3TabPrefix}${TAB_B}`); // 属主已死：租约缺失，孤儿回收。
    const third = adapterFor(TAB_C);
    const reprocessed = await third.reconcileRecoveryCapsules(storage, makeSession(TAB_C));
    expect(reprocessed.recovered).toHaveLength(1);
    expect(reprocessed.recovered[0]?.id).toBe(copyId);
    expect(snapshotKeys(storage, copyId)).toHaveLength(1);
    expect(readHead(storage, copyId)).toMatchObject({ revision: `1.${TAB_B}` });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();

    // 胶囊已删：再对账完全为空。
    const empty = await third.reconcileRecoveryCapsules(storage, makeSession(TAB_C));
    expect(empty).toEqual({ committed: [], recovered: [], failed: [] });
  });

  it("a tombstoned conversation becomes a recovery copy; no head/snapshot for the tombstoned id", async () => {
    const storage = new MemoryStorage();
    const adapterA = adapterFor(TAB_A);
    const sessionA = makeSession(TAB_A);
    const writer = adapterFor(TAB_B, "writer-b");
    const sessionB = makeSession(TAB_B);
    adapterA.save(makeState("alpha", [makeConversation("alpha", "初版"), makeConversation("beta", "二")]), storage, sessionA);
    const loadedB = writer.load(storage, sessionB);
    const dirty = editConversation(loadedB.conversations.find((conversation) => conversation.id === "alpha") as Conversation, "B 的未提交修改");
    expect(writer.writeRecoveryCapsule(makeState("beta", [loadedB.conversations.find((conversation) => conversation.id === "beta") as Conversation, dirty]), storage, sessionB)).toBeNull();
    // 另一标签页删除 alpha：tombstone 先于 head 移除落盘。
    const deleted = await adapterA.deleteConversationArbitrated("alpha", storage, sessionA);
    expect(deleted).toMatchObject({ ok: true });

    const reloadedB = adapterFor(TAB_B, "reloaded-b");
    const outcome = await reloadedB.reconcileRecoveryCapsules(storage, sessionB);

    const copyId = recoveredCopyIdV3("alpha", TAB_B, 1);
    expect(outcome.recovered).toHaveLength(1);
    expect(outcome.recovered[0]).toMatchObject({ id: copyId, title: "会话 alpha（恢复副本）" });
    // tombstoned id 绝不复活：无 head、无快照，tombstone 原样保留。
    expect(storage.getItem(sessionHeadKeyV3("alpha"))).toBeNull();
    expect(storage.getItem(sessionTombstoneKeyV3("alpha"))).not.toBeNull();
    const alphaSnapshots = [...storage.values.keys()].filter((key) => key.startsWith(sessionSnapshotKeyV3("alpha", "")));
    expect(alphaSnapshots).toHaveLength(1);
    expect(alphaSnapshots[0]?.startsWith(sessionSnapshotKeyV3(copyId, ""))).toBe(true);
    expect(readHead(storage, copyId)).toMatchObject({ revision: `1.${TAB_B}` });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
  });

  it("keeps the capsule for retry when an entry cannot be committed, and the retry converges", async () => {
    const storage = new MemoryStorage();
    const writer = adapterFor(TAB_B, "writer-b");
    const sessionB = makeSession(TAB_B);
    const dirty = makeDirty(writer, storage, sessionB, makeConversation("alpha", "初版"), "崩溃前的修改");
    expect(writer.writeRecoveryCapsule(makeState("alpha", [dirty]), storage, sessionB)).toBeNull();

    // 对账时存储仍不可写：条目未耐久 ⇒ 胶囊保留，失败进入 outcome。
    storage.failOnSet = (key) => key.startsWith(conversationStorageKeys.v3SnapshotPrefix);
    const reloaded = adapterFor(TAB_B, "reloaded-b");
    const blocked = await reloaded.reconcileRecoveryCapsules(storage, sessionB);
    expect(blocked.failed).toHaveLength(1);
    expect(blocked.committed).toEqual([]);
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).not.toBeNull();
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `1.${TAB_B}` });

    // 存储恢复后重试：幂等补交，胶囊删除。
    storage.failOnSet = null;
    const retry = await adapterFor(TAB_B, "retry-b").reconcileRecoveryCapsules(storage, sessionB);
    expect(retry.failed).toEqual([]);
    expect(retry.committed).toHaveLength(1);
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_B}` });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
  });

  it("an already-committed capsule entry converges by digest instead of duplicating", async () => {
    const storage = new MemoryStorage();
    const writer = adapterFor(TAB_B, "writer-b");
    const sessionB = makeSession(TAB_B);
    const dirty = makeDirty(writer, storage, sessionB, makeConversation("alpha", "初版"), "崩溃前的修改");
    expect(writer.writeRecoveryCapsule(makeState("alpha", [dirty]), storage, sessionB)).toBeNull();
    const capsuleRaw = storage.getItem(sessionRecoveryKeyV3(TAB_B)) as string;

    // 第一次对账补交落地，但胶囊删除前标签页再次死亡：胶囊原样残留。
    const first = await adapterFor(TAB_B, "first-b").reconcileRecoveryCapsules(storage, sessionB);
    expect(first.committed).toHaveLength(1);
    storage.setItem(sessionRecoveryKeyV3(TAB_B), capsuleRaw);

    // 重处理：head 内容已与胶囊一致（digest 收敛），绝不重复提交、绝不物化副本。
    const second = await adapterFor(TAB_B, "second-b").reconcileRecoveryCapsules(storage, sessionB);
    expect(second.recovered).toEqual([]);
    expect(second.committed).toHaveLength(1);
    expect(second.committed[0]).toMatchObject({ conversationId: "alpha", revision: `2.${TAB_B}` });
    expect(snapshotKeys(storage, "alpha")).toHaveLength(2); // 初版 + 补交各一份，没有第三份
    expect(readHead(storage, "alpha")).toMatchObject({ revision: `2.${TAB_B}` });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
  });
});

describe("foreign (orphaned) capsules", () => {
  it("reclaims a capsule whose owner lease is absent", async () => {
    const storage = new MemoryStorage();
    const writer = adapterFor(TAB_B, "orphan-b");
    const fresh = makeConversation("gamma", "孤儿内容");
    expect(writer.writeRecoveryCapsule(makeState("gamma", [fresh]), storage, makeSession(TAB_B))).toBeNull();

    // 新标签页（新 tabId）：属主租约缺失 ⇒ 回收孤儿胶囊。
    const reclaimer = adapterFor(TAB_C, "reclaimer-c");
    const outcome = await reclaimer.reconcileRecoveryCapsules(storage, makeSession(TAB_C));

    expect(outcome.committed).toHaveLength(1);
    expect(outcome.committed[0]).toMatchObject({ conversationId: "gamma", revision: `1.${TAB_C}` });
    expect(readHead(storage, "gamma")).toMatchObject({ revision: `1.${TAB_C}`, writerId: TAB_C });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
  });

  it("reclaims a capsule whose owner lease is stale", async () => {
    const storage = new MemoryStorage();
    const writer = adapterFor(TAB_B, "orphan-b");
    const fresh = makeConversation("gamma", "孤儿内容");
    expect(writer.writeRecoveryCapsule(makeState("gamma", [fresh]), storage, makeSession(TAB_B))).toBeNull();
    seedLease(storage, TAB_B, Date.now() - CONVERSATION_TOMBSTONE_LIMITS.tabLeaseStaleMs - 1000);

    const outcome = await adapterFor(TAB_C, "reclaimer-c").reconcileRecoveryCapsules(storage, makeSession(TAB_C));

    expect(outcome.committed).toHaveLength(1);
    expect(readHead(storage, "gamma")).toMatchObject({ revision: `1.${TAB_C}` });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
  });

  it("leaves a capsule with a LIVE owner lease to its owner (BFCache restore case)", async () => {
    const storage = new MemoryStorage();
    const writer = adapterFor(TAB_B, "owner-b");
    const fresh = makeConversation("gamma", "属主还活着");
    expect(writer.writeRecoveryCapsule(makeState("gamma", [fresh]), storage, makeSession(TAB_B))).toBeNull();
    seedLease(storage, TAB_B, Date.now());

    const outcome = await adapterFor(TAB_C, "observer-c").reconcileRecoveryCapsules(storage, makeSession(TAB_C));

    expect(outcome).toEqual({ committed: [], recovered: [], failed: [] });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).not.toBeNull();
    expect(storage.getItem(sessionHeadKeyV3("gamma"))).toBeNull();
  });

  it("quarantines an unparseable foreign capsule instead of deleting it silently", async () => {
    const storage = new MemoryStorage();
    storage.setItem(sessionRecoveryKeyV3(TAB_B), "garbage{{{");

    const outcome = await adapterFor(TAB_C, "observer-c").reconcileRecoveryCapsules(storage, makeSession(TAB_C));

    expect(outcome).toMatchObject({
      committed: [],
      recovered: [],
      failed: [expect.objectContaining({ code: "verification-failed" })],
    });
    expect(storage.getItem(sessionRecoveryKeyV3(TAB_B))).toBeNull();
    expect([...storage.values.keys()].some((key) => key.startsWith(`${conversationStorageKeys.v3QuarantinePrefix}capsule.${TAB_B}.`)))
      .toBe(true);
  });
});
