import { describe, expect, it } from "vitest";

import type { FrontendBackupEnvelopeV1 } from "../../api/workspaceBackupApi";
import type { ChatMessage } from "../chat/types";
import {
  checkpointDigest,
  createConversationPersistenceAdapter,
  readActiveWorkspaceEpoch,
  WORKSPACE_ACTIVE_EPOCH_KEY,
  WORKSPACE_RESTORE_FENCE_KEY,
  type ConversationCheckpointV3,
  type StorageLike,
} from "./persistence";
import {
  activateRestoreEpoch,
  beginWorkspaceRestoreFence,
  stageRestoreEnvelope,
  verifyRestoreEpoch,
} from "./restorePersistence";
import type { Conversation, PersistedConversationState } from "./types";

class MemoryStorage implements StorageLike {
  readonly values = new Map<string, string>();
  failOnSet: ((key: string) => boolean) | null = null;
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) {
    if (this.failOnSet?.(key)) throw new Error(`injected write failure: ${key}`);
    this.values.set(key, value);
  }
  removeItem(key: string) { this.values.delete(key); }
  get length() { return this.values.size; }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
}

function conversation(id: string, content: string): Conversation {
  const message: ChatMessage = {
    id: `${id}-message`,
    role: "user",
    content,
    reasoning: "",
    createdAt: 1,
    phase: "done",
    streaming: false,
    attachments: [],
    timeline: [],
    systemNotes: [],
  };
  return {
    id,
    title: id,
    messages: [message],
    model: "deepseek-v4-pro",
    thinkingEnabled: false,
    createdAt: 1,
    updatedAt: 2,
  };
}

function envelope(value: Conversation): FrontendBackupEnvelopeV1 {
  const digest = checkpointDigest(JSON.stringify(value));
  const checkpoint: ConversationCheckpointV3 = {
    schemaVersion: 3,
    conversationId: value.id,
    revision: "99.source-writer",
    parentRevision: null,
    writerId: "source-writer",
    savedAt: 3,
    digest,
    conversation: value,
  };
  return {
    schemaVersion: 1,
    sourceVersion: "4.4.0",
    createdAt: 4,
    conversations: [{ conversationId: value.id, headRevision: checkpoint.revision, checkpoint }],
    conflicts: [],
    digest: "a".repeat(64),
  };
}

describe("workspace restore epoch persistence", () => {
  it("fences a loaded replica and writes dirty content only to its previous epoch capsule", () => {
    const storage = new MemoryStorage();
    const session = new MemoryStorage();
    const adapter = createConversationPersistenceAdapter({
      locks: null,
      identity: { writerSessionId: "old-writer", documentInstanceId: "old-document" },
    });
    const original = conversation("old", "before restore");
    const initial: PersistedConversationState = { schemaVersion: 1, currentConversationId: "old", conversations: [original] };
    expect(adapter.save(initial, storage, session).ok).toBe(true);
    adapter.load(storage, session);

    beginWorkspaceRestoreFence("restore_fence", "epoch-target", "owner", storage);
    const dirty = { ...original, title: "dirty old tab", updatedAt: 5 };
    const state: PersistedConversationState = { ...initial, conversations: [dirty] };
    expect(adapter.save(state, storage, session)).toMatchObject({ ok: false, code: "restore-fenced" });
    expect(adapter.writeRecoveryCapsule(state, storage, session)).toBeNull();
    const capsule = [...storage.values.entries()].find(([key]) => key.startsWith("deepseek-infra.session.v3.recovery."));
    expect(capsule?.[1]).toContain("dirty old tab");
    expect([...storage.values.keys()].some((key) => key.startsWith("deepseek-infra.session.v4.epoch-target.head."))).toBe(false);

    storage.setItem(WORKSPACE_ACTIVE_EPOCH_KEY, "epoch-target");
    storage.removeItem(WORKSPACE_RESTORE_FENCE_KEY);
    const restoredAdapter = createConversationPersistenceAdapter({
      locks: null,
      identity: { writerSessionId: "new-writer", documentInstanceId: "new-document" },
    });
    restoredAdapter.load(storage, session);
    return restoredAdapter.reconcileRecoveryCapsules(storage, session).then((outcome) => {
      expect(outcome.recovered).toHaveLength(1);
      expect(outcome.recovered[0]?.title).toContain("恢复副本");
      expect(storage.getItem(capsule?.[0] ?? "")).toBeNull();
    });
  });

  it("does not activate on any staged write failure, then retries with fresh writer revisions and one pointer switch", async () => {
    const storage = new MemoryStorage();
    const session = new MemoryStorage();
    const restoreId = "restore_atomic";
    beginWorkspaceRestoreFence(restoreId, "epoch-target", "owner", storage);
    storage.failOnSet = (key) => key.includes(".head.");
    await expect(stageRestoreEnvelope(
      envelope(conversation("imported", "restored")),
      { restoreId, targetEpoch: "epoch-target", serverTransactionDigest: "b".repeat(64) },
      storage,
      session,
    )).rejects.toThrow("injected write failure");
    expect(readActiveWorkspaceEpoch(storage)).toBe("legacy");
    expect(storage.getItem(WORKSPACE_ACTIVE_EPOCH_KEY)).toBeNull();

    storage.failOnSet = null;
    const staged = await stageRestoreEnvelope(
      envelope(conversation("imported", "restored")),
      { restoreId, targetEpoch: "epoch-target", serverTransactionDigest: "b".repeat(64) },
      storage,
      session,
    );
    expect(staged.stagedHeads).toBe(1);
    expect(await verifyRestoreEpoch(restoreId, storage, session)).toBe(staged.digest);
    activateRestoreEpoch(restoreId, storage);
    expect(readActiveWorkspaceEpoch(storage)).toBe("epoch-target");

    const loaded = createConversationPersistenceAdapter().load(storage, session);
    expect(loaded.conversations.map((item) => item.id)).toEqual(["imported"]);
    const snapshot = [...storage.values.entries()].find(([key]) => key.includes(".snapshot.imported."));
    expect(snapshot?.[0]).not.toContain("99.source-writer");
    expect(snapshot?.[1]).not.toContain('"writerId":"source-writer"');
  });
});
