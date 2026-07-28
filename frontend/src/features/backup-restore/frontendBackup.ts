import {
  checkpointDigest,
  conversationStorageKeys,
  createConversationPersistenceAdapter,
  parseDurableConflictBranch,
  sessionConflictIndexKeyV3,
  sessionConflictKeyV3,
  sessionHeadKeyV3,
  sessionSnapshotKeyV3,
  type ConversationCheckpointV3,
  type StorageLike,
} from "../../domain/conversation/persistence";
import type { PersistedConversationState } from "../../domain/conversation/types";
import type { FrontendBackupEnvelopeV1 } from "../../api/workspaceBackupApi";
import { saveComposerDraft, type ComposerDraft } from "../composer/composerDraftPersistence";

const DRAFT_PREFIX = "deepseek:composer-draft:";
const RESTORE_EPOCH_KEY = "deepseek-infra.restore-epoch";
const RESTORE_CHANNEL = "deepseek-infra-workspace-restore";

function keys(storage: StorageLike, prefix: string): string[] {
  const result: string[] = [];
  if (typeof storage.length !== "number" || typeof storage.key !== "function") return result;
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key?.startsWith(prefix)) result.push(key);
  }
  return result.sort();
}

function parseObject(raw: string | null): Record<string, unknown> | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableValue(item)]),
    );
  }
  return value;
}

async function sha256(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(stableValue(value)));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function verifiedCheckpoint(raw: string | null, conversationId: string, revision: string): ConversationCheckpointV3 | null {
  const value = parseObject(raw) as Partial<ConversationCheckpointV3> | null;
  if (!value || value.schemaVersion !== 3 || value.conversationId !== conversationId || value.revision !== revision) return null;
  if (!value.conversation || checkpointDigest(JSON.stringify(value.conversation)) !== value.digest) return null;
  return value as ConversationCheckpointV3;
}

export async function collectFrontendBackupEnvelope(
  sourceVersion: string,
  includeDrafts: boolean,
  storage: StorageLike = window.localStorage,
  session: StorageLike = window.sessionStorage,
): Promise<FrontendBackupEnvelopeV1> {
  const conversations: FrontendBackupEnvelopeV1["conversations"] = [];
  for (const headKey of keys(storage, conversationStorageKeys.v3HeadPrefix)) {
    const conversationId = headKey.slice(conversationStorageKeys.v3HeadPrefix.length);
    const head = parseObject(storage.getItem(headKey));
    const revision = typeof head?.revision === "string" ? head.revision : "";
    const checkpoint = verifiedCheckpoint(storage.getItem(sessionSnapshotKeyV3(conversationId, revision)), conversationId, revision);
    if (!checkpoint || checkpoint.digest !== head?.digest) {
      throw new Error(`会话 ${conversationId} 的共享 Head 未通过摘要校验`);
    }
    const { writerId: _writerId, ...portableCheckpoint } = checkpoint;
    conversations.push({ conversationId, headRevision: revision, checkpoint: portableCheckpoint });
  }
  const conflicts = keys(storage, conversationStorageKeys.v3ConflictPrefix)
    .map((key) => parseDurableConflictBranch(storage.getItem(key)))
    .filter((value) => value !== null)
    .flatMap(({ writerSessionId: _writerSessionId, ...branch }) => {
      const checkpoint = verifiedCheckpoint(
        storage.getItem(sessionSnapshotKeyV3(branch.conversationId, branch.branchRevision)),
        branch.conversationId,
        branch.branchRevision,
      );
      if (!checkpoint) throw new Error(`冲突 ${branch.conflictId} 的分支快照未通过摘要校验`);
      const { writerId: _writerId, ...portableCheckpoint } = checkpoint;
      return [{ branch, checkpoint: portableCheckpoint }];
    });
  const drafts = includeDrafts
    ? keys(session, DRAFT_PREFIX).flatMap((key) => {
      const value = parseObject(session.getItem(key));
      return value ? [value] : [];
    })
    : undefined;
  const unsigned = {
    schemaVersion: 1 as const,
    sourceVersion,
    createdAt: Date.now(),
    conversations,
    conflicts,
    ...(drafts ? { drafts } : {}),
  };
  return { ...unsigned, digest: await sha256(unsigned) };
}

export async function applyFrontendBackupEnvelope(
  envelope: FrontendBackupEnvelopeV1,
  restoreEpoch: number,
  storage: StorageLike = window.localStorage,
  session: StorageLike = window.sessionStorage,
): Promise<{ imported: number; remapped: number }> {
  const { digest, ...unsigned } = envelope;
  if (envelope.schemaVersion !== 1 || await sha256(unsigned) !== digest) {
    throw new Error("浏览器恢复信封摘要无效");
  }
  const persistence = createConversationPersistenceAdapter();
  const existing = persistence.load(storage, session);
  const imported = [...existing.conversations];
  const remappedIds = new Map<string, string>();
  let remapped = 0;
  for (const entry of envelope.conversations) {
    const checkpoint = entry.checkpoint as Partial<ConversationCheckpointV3>;
    if (!checkpoint.conversation || checkpointDigest(JSON.stringify(checkpoint.conversation)) !== checkpoint.digest) {
      throw new Error(`会话 ${entry.conversationId} 的恢复快照无效`);
    }
    const previous = imported.find((conversation) => conversation.id === entry.conversationId);
    if (previous && checkpointDigest(JSON.stringify(previous)) === checkpoint.digest) continue;
    const conversation = structuredClone(checkpoint.conversation);
    if (previous) {
      conversation.id = `${entry.conversationId}.imported.${String(checkpoint.digest).slice(0, 12)}`;
      conversation.title = `${conversation.title}（恢复副本）`;
      remappedIds.set(entry.conversationId, conversation.id);
      remapped += 1;
    }
    imported.push(conversation);
  }
  const state: PersistedConversationState = {
    schemaVersion: 1,
    currentConversationId: existing.currentConversationId ?? imported[0]?.id ?? null,
    conversations: imported,
  };
  const result = await persistence.saveArbitrated(() => state, storage, session);
  if (!result.ok) throw new Error(result.message);
  for (const rawConflict of envelope.conflicts) {
    if (!rawConflict || typeof rawConflict !== "object") continue;
    const value = rawConflict as {
      branch?: Record<string, unknown>;
      checkpoint?: Partial<ConversationCheckpointV3>;
    };
    const branchSource = value.branch;
    const originalConversationId = typeof branchSource?.conversationId === "string" ? branchSource.conversationId : "";
    const originalConflictId = typeof branchSource?.conflictId === "string" ? branchSource.conflictId : "";
    if (!originalConversationId || !originalConflictId || !value.checkpoint?.conversation) continue;
    const conversationId = remappedIds.get(originalConversationId) ?? originalConversationId;
    const conversation = structuredClone(value.checkpoint.conversation);
    conversation.id = conversationId;
    const digest = checkpointDigest(JSON.stringify(conversation));
    const branchRevision = typeof branchSource?.branchRevision === "string" ? branchSource.branchRevision : `1.restore-${restoreEpoch}`;
    const head = parseObject(storage.getItem(sessionHeadKeyV3(conversationId)));
    const sharedRevision = typeof head?.revision === "string" ? head.revision : branchRevision;
    const conflictId = checkpointDigest(`${originalConflictId}\0${conversationId}\0${restoreEpoch}`);
    const checkpoint = {
      ...value.checkpoint,
      schemaVersion: 3,
      conversationId,
      revision: branchRevision,
      writerId: "restore",
      digest,
      conversation,
    };
    const branch = {
      ...branchSource,
      schemaVersion: 1,
      conflictId,
      conversationId,
      branchRevision,
      parentBranchRevision: null,
      baseRevision: sharedRevision,
      sharedRevision,
      writerSessionId: "restore",
      status: "pending",
      updatedAt: Date.now(),
    };
    storage.setItem(sessionSnapshotKeyV3(conversationId, branchRevision), JSON.stringify(checkpoint));
    storage.setItem(sessionConflictKeyV3(conversationId, conflictId), JSON.stringify(branch));
    storage.setItem(sessionConflictKeyV3(conversationId), JSON.stringify({
      revision: branchRevision,
      baseRevision: sharedRevision,
      sharedRevision,
      writerId: "restore",
      savedAt: branch.updatedAt,
    }));
    const indexKey = sessionConflictIndexKeyV3(conversationId);
    const index = parseObject(storage.getItem(indexKey));
    const conflictIds = Array.isArray(index?.conflictIds)
      ? index.conflictIds.filter((item): item is string => typeof item === "string")
      : [];
    if (!conflictIds.includes(conflictId)) conflictIds.push(conflictId);
    storage.setItem(indexKey, JSON.stringify({ schemaVersion: 1, conflictIds }));
    if (storage.getItem(sessionConflictKeyV3(conversationId, conflictId)) !== JSON.stringify(branch)) {
      throw new Error(`冲突 ${originalConflictId} 写入后回读核验失败`);
    }
  }
  for (const rawDraft of envelope.drafts ?? []) {
    if (!rawDraft || typeof rawDraft !== "object") continue;
    const value = rawDraft as Partial<ComposerDraft>;
    if (typeof value.conversationId !== "string" || typeof value.text !== "string" || typeof value.updatedAt !== "number") continue;
    const restored = saveComposerDraft({
      conversationId: remappedIds.get(value.conversationId) ?? value.conversationId,
      projectId: typeof value.projectId === "string" ? value.projectId : null,
      text: value.text,
      updatedAt: value.updatedAt,
    }, session);
    if (!restored.ok) throw new Error(restored.message);
  }
  storage.setItem(RESTORE_EPOCH_KEY, String(restoreEpoch));
  try {
    new BroadcastChannel(RESTORE_CHANNEL).postMessage({ type: "restore_epoch_changed", restoreEpoch });
  } catch {
    // Storage event remains the fallback for environments without BroadcastChannel.
  }
  return { imported: envelope.conversations.length, remapped };
}

export function listenForRestoreEpoch(onChanged: (epoch: number) => void): () => void {
  let channel: BroadcastChannel | null = null;
  try {
    channel = new BroadcastChannel(RESTORE_CHANNEL);
    channel.onmessage = (event) => {
      if (event.data?.type === "restore_epoch_changed" && typeof event.data.restoreEpoch === "number") {
        onChanged(event.data.restoreEpoch);
      }
    };
  } catch {
    channel = null;
  }
  const onStorage = (event: StorageEvent) => {
    if (event.key === RESTORE_EPOCH_KEY && event.newValue) onChanged(Number(event.newValue));
  };
  window.addEventListener("storage", onStorage);
  return () => {
    channel?.close();
    window.removeEventListener("storage", onStorage);
  };
}
