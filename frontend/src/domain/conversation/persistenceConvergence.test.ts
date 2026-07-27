import { describe, expect, it, vi } from "vitest";

import { TAB_ID_STORAGE_KEY } from "../../app/tabIdentity";
import type { ChatMessage } from "../chat/types";
import type { Conversation } from "./types";
import {
  CONVERSATION_CHECKPOINT_LOCK_NAME,
  conversationStorageKeys,
  createConversationPersistenceAdapter,
  parseRecoveryCapsule,
  runIdleCheckpointGc,
  sessionConflictIndexKeyV3,
  sessionConflictKeyV3,
  sessionHeadKeyV3,
  sessionProposalKeyV3,
  sessionRecoveryKeyV3,
  sessionSnapshotKeyV3,
  type LockRequestOptions,
  type LocksLike,
  type StorageLike,
} from "./persistence";

const WRITER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const WRITER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const WRITER_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

class MemoryStorage implements StorageLike {
  readonly values = new Map<string, string>();
  failOnSet: ((key: string) => boolean) | null = null;
  corruptOnSet: ((key: string) => boolean) | null = null;
  quotaOnSet: ((key: string, value: string) => boolean) | null = null;
  beforeSet: ((key: string, value: string) => void) | null = null;
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) {
    this.beforeSet?.(key, value);
    if (this.quotaOnSet?.(key, value)) throw Object.assign(new Error("quota"), { name: "QuotaExceededError" });
    if (this.failOnSet?.(key)) throw new Error("setItem failed");
    this.values.set(key, this.corruptOnSet?.(key) ? `${value}#corrupt` : value);
  }
  removeItem(key: string) { this.values.delete(key); }
  get length(): number { return this.values.size; }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null; }
}

/**
 * 模拟 Chromium 不同 renderer 的 localStorage 可见性：写入方可立即回读自己的
 * Proposal，但同一同步提交栈的枚举只看见最后一次本地写；两个提交返回后才发布
 * 全部 Proposal。两个 Writer 因此会先各自推进 Head，随后必须靠最终仲裁收敛。
 */
class DelayedProposalStorage extends MemoryStorage {
  private readonly pendingProposals = new Map<string, string>();
  private lastPendingProposal: string | null = null;

  override getItem(key: string): string | null {
    return this.pendingProposals.get(key) ?? super.getItem(key);
  }

  override setItem(key: string, value: string): void {
    if (!key.startsWith(conversationStorageKeys.v3ProposalPrefix)) {
      super.setItem(key, value);
      return;
    }
    this.pendingProposals.set(key, value);
    this.lastPendingProposal = key;
    globalThis.queueMicrotask(() => {
      const pending = this.pendingProposals.get(key);
      if (pending === undefined) return;
      this.pendingProposals.delete(key);
      this.values.set(key, pending);
      if (this.lastPendingProposal === key) this.lastPendingProposal = null;
    });
  }

  override get length(): number {
    return this.visibleKeys().length;
  }

  override key(index: number): string | null {
    return this.visibleKeys()[index] ?? null;
  }

  private visibleKeys(): string[] {
    const keys = [...this.values.keys()];
    if (this.lastPendingProposal && !this.values.has(this.lastPendingProposal)) keys.push(this.lastPendingProposal);
    return keys;
  }
}

class Mutex implements LocksLike {
  private tail: Promise<unknown> = Promise.resolve();
  request<T>(_name: string, _options: LockRequestOptions, callback: () => T | Promise<T>): Promise<T> {
    const run = this.tail.then(callback);
    this.tail = run.catch(() => undefined);
    return run;
  }
}

function session(continuityId = "deadbeef"): MemoryStorage {
  const value = new MemoryStorage();
  value.setItem(TAB_ID_STORAGE_KEY, continuityId);
  return value;
}

function adapter(writerSessionId: string, locks?: LocksLike | null) {
  return createConversationPersistenceAdapter({
    locks,
    identity: { writerSessionId, documentInstanceId: `document-${writerSessionId}` },
  });
}

function message(id: string, content: string): ChatMessage {
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

function conversation(id: string, content: string): Conversation {
  return {
    id,
    title: `会话 ${id}`,
    messages: [message(`${id}-0`, content)],
    model: "deepseek-v4-pro",
    thinkingEnabled: false,
    createdAt: 100,
    updatedAt: 100,
  };
}

function edit(value: Conversation, content: string): Conversation {
  return {
    ...value,
    messages: [...value.messages, message(`${value.id}-${value.messages.length}`, content)],
    updatedAt: value.updatedAt + 1,
  };
}

function state(values: Conversation[]) {
  return { schemaVersion: 1 as const, currentConversationId: values[0]?.id ?? null, conversations: values };
}

function head(storage: MemoryStorage, conversationId: string): Record<string, unknown> {
  return JSON.parse(storage.getItem(sessionHeadKeyV3(conversationId)) ?? "{}") as Record<string, unknown>;
}

describe("4.3.7 replica convergence", () => {
  it("gives two document instances different UUID writers while preserving the same continuity selection", () => {
    const sharedSession = session();
    const first = createConversationPersistenceAdapter();
    const second = createConversationPersistenceAdapter();

    const firstIdentity = first.getReplicaIdentity(sharedSession);
    const secondIdentity = second.getReplicaIdentity(sharedSession);

    expect(firstIdentity.tabContinuityId).toBe(secondIdentity.tabContinuityId);
    expect(firstIdentity.writerSessionId).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/);
    expect(secondIdentity.writerSessionId).not.toBe(firstIdentity.writerSessionId);
    expect(secondIdentity.documentInstanceId).not.toBe(firstIdentity.documentInstanceId);

    const rotated = first.rotateWriterIdentity(sharedSession);
    expect(rotated.tabContinuityId).toBe(firstIdentity.tabContinuityId);
    expect(rotated.documentInstanceId).toBe(firstIdentity.documentInstanceId);
    expect(rotated.writerSessionId).not.toBe(firstIdentity.writerSessionId);
  });

  it("retains every concurrent loser and resolving one ledger entry leaves the others intact", async () => {
    const storage = new MemoryStorage();
    const lock = new Mutex();
    const a = adapter(WRITER_A, lock);
    const b = adapter(WRITER_B, lock);
    const c = adapter(WRITER_C, lock);
    const sessionA = session("aaaa0001");
    const sessionB = session("bbbb0002");
    const sessionC = session("cccc0003");
    const initial = conversation("alpha", "base");
    a.save(state([initial]), storage, sessionA);
    const baseB = b.load(storage, sessionB).conversations[0] as Conversation;
    const baseC = c.load(storage, sessionC).conversations[0] as Conversation;

    await a.saveArbitrated(() => state([edit(initial, "winner")]), storage, sessionA);
    await b.saveArbitrated(() => state([edit(baseB, "branch-b")]), storage, sessionB);
    await c.saveArbitrated(() => state([edit(baseC, "branch-c")]), storage, sessionC);

    const branches = b.listConflictBranches("alpha", storage);
    expect(branches).toHaveLength(2);
    expect(new Set(branches.map((branch) => branch.writerSessionId))).toEqual(new Set([WRITER_B, WRITER_C]));
    expect(JSON.parse(storage.getItem(sessionConflictIndexKeyV3("alpha")) ?? "{}").conflictIds).toHaveLength(2);
    for (const branch of branches) {
      expect(storage.getItem(sessionConflictKeyV3("alpha", branch.conflictId))).not.toBeNull();
      expect(storage.getItem(sessionSnapshotKeyV3("alpha", branch.branchRevision))).not.toBeNull();
    }

    const discarded = await b.resolveConflictByReloadArbitrated("alpha", branches[0]?.conflictId as string, storage);
    expect(discarded.ok).toBe(true);
    expect(b.listConflictBranches("alpha", storage)).toHaveLength(1);
    expect(b.listConflictBranches("alpha", storage)[0]?.conflictId).toBe(branches[1]?.conflictId);
  });

  it("never replaces an isolated conflict branch when the shared Head advances again", async () => {
    const storage = new MemoryStorage();
    const lock = new Mutex();
    const a = adapter(WRITER_A, lock);
    const b = adapter(WRITER_B, lock);
    const sessionA = session("aaaa0001");
    const sessionB = session("bbbb0002");
    const initial = conversation("alpha", "base");
    a.save(state([initial]), storage, sessionA);
    const baseB = b.load(storage, sessionB).conversations[0] as Conversation;
    const winner = edit(initial, "winner");
    const branch = edit(baseB, "branch-b");
    await a.saveArbitrated(() => state([winner]), storage, sessionA);
    await b.saveArbitrated(() => state([branch]), storage, sessionB);

    const winnerAgain = edit(winner, "winner-again");
    await a.saveArbitrated(() => state([winnerAgain]), storage, sessionA);
    expect(b.reconcileRemoteCommit("alpha", branch, storage)).toEqual({ kind: "stale" });

    const branchAgain = edit(branch, "branch-b-again");
    await b.saveArbitrated(() => state([branchAgain]), storage, sessionB);
    expect(b.readSharedConversation("alpha", storage)?.conversation.messages.at(-1)?.content).toBe("winner-again");
    const durable = b.listConflictBranches("alpha", storage)[0];
    expect(durable).toBeDefined();
    expect(b.readConflictBranch("alpha", storage, durable?.conflictId)?.conversation.messages.at(-1)?.content)
      .toBe("branch-b-again");
  });

  it("materializes a stable conflict copy before releasing the branch and retries without duplicates", async () => {
    const storage = new MemoryStorage();
    const lock = new Mutex();
    const a = adapter(WRITER_A, lock);
    const b = adapter(WRITER_B, lock);
    const sessionA = session("aaaa0001");
    const sessionB = session("bbbb0002");
    const initial = conversation("alpha", "base");
    a.save(state([initial]), storage, sessionA);
    const baseB = b.load(storage, sessionB).conversations[0] as Conversation;
    await a.saveArbitrated(() => state([edit(initial, "winner")]), storage, sessionA);
    await b.saveArbitrated(() => state([edit(baseB, "loser")]), storage, sessionB);
    const branch = b.listConflictBranches("alpha", storage)[0] as NonNullable<ReturnType<typeof b.listConflictBranches>[number]>;
    const copyId = `alpha.conflict.${branch.conflictId}`;

    storage.failOnSet = (key) => key === sessionHeadKeyV3(copyId);
    const failed = await b.resolveConflictByCopyArbitrated("alpha", branch.conflictId, storage, sessionB);
    expect(failed.ok).toBe(false);
    expect(storage.getItem(sessionConflictKeyV3("alpha", branch.conflictId))).not.toBeNull();
    expect(storage.getItem(sessionHeadKeyV3(copyId))).toBeNull();
    expect(storage.getItem(sessionSnapshotKeyV3(copyId, `1.${WRITER_B}`))).not.toBeNull();

    storage.failOnSet = null;
    const resolved = await b.resolveConflictByCopyArbitrated("alpha", branch.conflictId, storage, sessionB);
    expect(resolved.ok).toBe(true);
    expect(storage.getItem(sessionHeadKeyV3(copyId))).not.toBeNull();
    expect(storage.getItem(sessionConflictKeyV3("alpha", branch.conflictId))).toBeNull();
    const snapshotsAfterSuccess = [...storage.values.keys()].filter((key) => key.startsWith(sessionSnapshotKeyV3(copyId, "")));

    const retried = await b.resolveConflictByCopyArbitrated("alpha", branch.conflictId, storage, sessionB);
    expect(retried.ok).toBe(true);
    expect([...storage.values.keys()].filter((key) => key.startsWith(sessionSnapshotKeyV3(copyId, ""))))
      .toEqual(snapshotsAfterSuccess);
  });

  it("converges truly interleaved lock-free sibling proposals without losing either snapshot", () => {
    const storage = new MemoryStorage();
    const a = adapter(WRITER_A, null);
    const b = adapter(WRITER_B, null);
    const sessionA = session("aaaa0001");
    const sessionB = session("bbbb0002");
    const initial = conversation("alpha", "base");
    a.save(state([initial]), storage, sessionA);
    const baseB = b.load(storage, sessionB).conversations[0] as Conversation;
    const editA = edit(initial, "proposal-a");
    const editB = edit(baseB, "proposal-b");
    let resultB: ReturnType<typeof b.save> | null = null;
    const proposalA = sessionProposalKeyV3("alpha", `1.${WRITER_A}`, `2.${WRITER_A}`);

    storage.beforeSet = (key) => {
      if (key !== proposalA) return;
      storage.beforeSet = null;
      resultB = b.save(state([editB]), storage, sessionB);
    };
    const resultA = a.save(state([editA]), storage, sessionA);

    expect(resultA.ok).toBe(true);
    expect(resultB).toMatchObject({ ok: true });
    expect(head(storage, "alpha")).toMatchObject({ revision: `2.${WRITER_A}`, writerId: WRITER_A });
    expect(storage.getItem(sessionSnapshotKeyV3("alpha", `2.${WRITER_A}`))).not.toBeNull();
    expect(storage.getItem(sessionSnapshotKeyV3("alpha", `2.${WRITER_B}`))).not.toBeNull();
    expect(a.listConflictBranches("alpha", storage)).toEqual([
      expect.objectContaining({ branchRevision: `2.${WRITER_B}`, writerSessionId: WRITER_B }),
    ]);
    expect(a.readSharedConversation("alpha", storage)?.revision).toBe(`2.${WRITER_A}`);
    expect(b.readSharedConversation("alpha", storage)?.revision).toBe(`2.${WRITER_A}`);
  });

  it("reconciles sibling proposals that become visible only after both lock-free commits", async () => {
    const storage = new DelayedProposalStorage();
    const a = adapter(WRITER_A, null);
    const b = adapter(WRITER_B, null);
    const sessionA = session("aaaa0001");
    const sessionB = session("bbbb0002");
    const initial = conversation("alpha", "base");
    a.save(state([initial]), storage, sessionA);
    const baseB = b.load(storage, sessionB).conversations[0] as Conversation;

    const [resultA, resultB] = await Promise.all([
      a.saveArbitrated(() => state([edit(initial, "proposal-a")]), storage, sessionA),
      b.saveArbitrated(() => state([edit(baseB, "proposal-b")]), storage, sessionB),
    ]);

    expect(resultA.ok).toBe(true);
    expect(resultB.ok).toBe(true);
    expect(head(storage, "alpha")).toMatchObject({ revision: `2.${WRITER_A}`, writerId: WRITER_A });
    expect(storage.getItem(sessionSnapshotKeyV3("alpha", `2.${WRITER_A}`))).not.toBeNull();
    // B 在自己的 Proposal 尚未公开时已看见 A 的临时 Head，因此 revision 序号
    // 可以更高；parent 仍是共同 base，最终仲裁必须把它保留为负方分支。
    expect(storage.getItem(sessionSnapshotKeyV3("alpha", `3.${WRITER_B}`))).not.toBeNull();
    expect(a.listConflictBranches("alpha", storage)).toEqual([
      expect.objectContaining({ branchRevision: `3.${WRITER_B}`, writerSessionId: WRITER_B }),
    ]);
    expect(a.readSharedConversation("alpha", storage)?.revision).toBe(`2.${WRITER_A}`);
    expect(b.readSharedConversation("alpha", storage)?.revision).toBe(`2.${WRITER_A}`);
  });

  it("never reruns a lock callback when the lock promise rejects after invoking it", async () => {
    let callbacks = 0;
    const rejectingLock: LocksLike = {
      async request<T>(name: string, _options: LockRequestOptions, callback: () => T | Promise<T>): Promise<T> {
        expect(name).toBe(CONVERSATION_CHECKPOINT_LOCK_NAME);
        callbacks += 1;
        await callback();
        throw new Error("lock transport failed after callback");
      },
    };
    const storage = new MemoryStorage();
    const value = conversation("alpha", "once");
    const getState = vi.fn(() => state([value]));
    const persistence = adapter(WRITER_A, rejectingLock);

    await expect(persistence.saveArbitrated(getState, storage, session())).rejects.toThrow("lock transport failed");
    expect(callbacks).toBe(1);
    expect(getState).toHaveBeenCalledTimes(1);
    expect([...storage.values.keys()].filter((key) => key.startsWith(sessionSnapshotKeyV3("alpha", "")))).toHaveLength(1);
  });

  it("does not advertise a live writer lease when the Head transaction fails", () => {
    const storage = new MemoryStorage();
    const persistence = adapter(WRITER_A, null);
    storage.failOnSet = (key) => key === sessionHeadKeyV3("alpha");

    const result = persistence.save(state([conversation("alpha", "uncommitted")]), storage, session());

    expect(result.ok).toBe(false);
    expect(storage.getItem(sessionSnapshotKeyV3("alpha", `1.${WRITER_A}`))).not.toBeNull();
    expect(storage.getItem(`${conversationStorageKeys.v3TabPrefix}${WRITER_A}`)).toBeNull();
  });

  it("loads a valid parent, protects it from GC, and self-heals the degraded Head under arbitration", async () => {
    const storage = new MemoryStorage();
    const sessionA = session("aaaa0001");
    const writer = adapter(WRITER_A);
    const initial = conversation("alpha", "valid-parent");
    writer.save(state([initial]), storage, sessionA);
    writer.save(state([edit(initial, "corrupt-head")]), storage, sessionA);
    storage.setItem(sessionSnapshotKeyV3("alpha", `2.${WRITER_A}`), "corrupt{{");

    const warnings: string[] = [];
    const reloaded = adapter(WRITER_B, new Mutex());
    const loaded = reloaded.load(storage, session("bbbb0002"));
    expect(loaded.conversations[0]?.messages.at(-1)?.content).toBe("valid-parent");
    runIdleCheckpointGc(storage, 16);
    expect(storage.getItem(sessionSnapshotKeyV3("alpha", `1.${WRITER_A}`))).not.toBeNull();

    await reloaded.saveArbitrated(() => loaded, storage, session("bbbb0002"), {
      onWarning: (warning) => warnings.push(warning.code),
    });
    expect(head(storage, "alpha")).toMatchObject({ revision: `1.${WRITER_A}` });
    expect(warnings).toEqual(["verification-failed"]);
    expect([...storage.values.keys()].some((key) =>
      key.startsWith(`${conversationStorageKeys.v3QuarantinePrefix}head.alpha.2.${WRITER_A}`))).toBe(true);
  });

  it("never resurrects an original id when a known base loses both Head and tombstone", () => {
    const storage = new MemoryStorage();
    const persistence = adapter(WRITER_A);
    const ownSession = session();
    const initial = conversation("alpha", "base");
    persistence.save(state([initial]), storage, ownSession);
    storage.removeItem(sessionHeadKeyV3("alpha"));
    const recoveries: Conversation[] = [];

    const result = persistence.save(state([edit(initial, "sleeping-tab")]), storage, ownSession, {
      onRecovery: ({ copy }) => recoveries.push(copy),
    });

    expect(result.ok).toBe(true);
    expect(storage.getItem(sessionHeadKeyV3("alpha"))).toBeNull();
    expect(recoveries).toHaveLength(1);
    expect(recoveries[0]?.id).not.toBe("alpha");
    expect(storage.getItem(sessionHeadKeyV3(recoveries[0]?.id as string))).not.toBeNull();
  });

  it("uses writer-specific lease and capsule keys even with one shared sessionStorage value", () => {
    const storage = new MemoryStorage();
    const sharedSession = session();
    const first = createConversationPersistenceAdapter();
    const second = createConversationPersistenceAdapter();
    const dirtyA = conversation("alpha", "a");
    const dirtyB = conversation("beta", "b");
    const firstWriter = first.getReplicaIdentity(sharedSession).writerSessionId;
    const secondWriter = second.getReplicaIdentity(sharedSession).writerSessionId;

    first.setTabLease(true, storage, sharedSession);
    second.setTabLease(true, storage, sharedSession);
    expect(first.writeRecoveryCapsule(state([dirtyA]), storage, sharedSession)).toBeNull();
    expect(second.writeRecoveryCapsule(state([dirtyB]), storage, sharedSession)).toBeNull();

    expect(storage.getItem(`${conversationStorageKeys.v3TabPrefix}${firstWriter}`)).not.toBeNull();
    expect(storage.getItem(`${conversationStorageKeys.v3TabPrefix}${secondWriter}`)).not.toBeNull();
    expect(storage.getItem(sessionRecoveryKeyV3(firstWriter))).not.toBeNull();
    expect(storage.getItem(sessionRecoveryKeyV3(secondWriter))).not.toBeNull();
  });

  it("fails a capsule whose write-back differs and never reports it as verified", () => {
    const storage = new MemoryStorage();
    const persistence = adapter(WRITER_A);
    storage.corruptOnSet = (key) => key === sessionRecoveryKeyV3(WRITER_A);

    const failure = persistence.writeRecoveryCapsule(state([conversation("alpha", "body")]), storage, session());

    expect(failure).toMatchObject({ ok: false, code: "verification-failed" });
    expect(parseRecoveryCapsule(storage.getItem(sessionRecoveryKeyV3(WRITER_A)))).toBeNull();
  });

  it("compacts oversized capsule previews deterministically while preserving every body byte", () => {
    const storage = new MemoryStorage();
    const persistence = adapter(WRITER_A);
    const body = "正文必须完整保留";
    const rich: Conversation = {
      ...conversation("alpha", body),
      messages: [{
        ...message("alpha-0", body),
        attachments: [{
          id: "preview",
          name: "large.png",
          type: "image/png",
          kind: "image",
          preview: `data:image/png;base64,${"A".repeat(64 * 1024)}`,
        }],
      }],
    };
    storage.quotaOnSet = (key, value) => key === sessionRecoveryKeyV3(WRITER_A) && value.length > 10_000;

    expect(persistence.writeRecoveryCapsule(state([rich]), storage, session())).toBeNull();
    const capsule = parseRecoveryCapsule(storage.getItem(sessionRecoveryKeyV3(WRITER_A)));

    expect(capsule?.entries[0]?.compaction).toMatchObject({ level: 1, reason: "storage-pressure" });
    expect(capsule?.entries[0]?.conversation.messages[0]?.content).toBe(body);
    expect(capsule?.entries[0]?.conversation.messages[0]?.attachments[0]?.preview).toBeUndefined();
  });
});
