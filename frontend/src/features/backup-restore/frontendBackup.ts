import {
  abortWorkspaceRestore,
  commitWorkspaceRestore,
  completeWorkspaceRestore,
  getWorkspaceRestore,
  markWorkspaceFrontendPrepared,
  prepareWorkspaceRestore,
  type FrontendBackupEnvelopeV1,
  type RestoreMode,
} from "../../api/workspaceBackupApi";
import {
  checkpointDigest,
  conversationStorageKeys,
  parseDurableConflictBranch,
  readActiveWorkspaceEpoch,
  sessionSnapshotKeyV3,
  workspaceEpochStorage,
  WORKSPACE_ACTIVE_EPOCH_KEY,
  WORKSPACE_EPOCH_PREFIX,
  WORKSPACE_RESTORE_FENCE_KEY,
  type ConversationCheckpointV3,
  type StorageLike,
} from "../../domain/conversation/persistence";
import {
  activateRestoreEpoch,
  beginWorkspaceRestoreFence,
  completeFrontendRestore,
  markServerCommitted,
  readFrontendRestoreJournal,
  RESTORE_CHANNEL,
  rollbackRestoreEpoch,
  stageRestoreEnvelope,
  verifyRestoreEpoch,
  type WorkspaceRestoreFence,
} from "../../domain/conversation/restorePersistence";

const LEGACY_DRAFT_PREFIX = "deepseek:composer-draft:";

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
  return [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, "0")).join("");
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
  const epoch = readActiveWorkspaceEpoch(storage);
  const scoped = workspaceEpochStorage(storage, epoch);
  const conversations: FrontendBackupEnvelopeV1["conversations"] = [];
  for (const headKey of keys(scoped, conversationStorageKeys.v3HeadPrefix)) {
    const conversationId = headKey.slice(conversationStorageKeys.v3HeadPrefix.length);
    const head = parseObject(scoped.getItem(headKey));
    const revision = typeof head?.revision === "string" ? head.revision : "";
    const checkpoint = verifiedCheckpoint(scoped.getItem(sessionSnapshotKeyV3(conversationId, revision)), conversationId, revision);
    if (!checkpoint || checkpoint.digest !== head?.digest) {
      throw new Error(`会话 ${conversationId} 的共享 Head 未通过摘要校验`);
    }
    const { writerId: _writerId, ...portableCheckpoint } = checkpoint;
    conversations.push({ conversationId, headRevision: revision, checkpoint: portableCheckpoint });
  }
  const conflicts = keys(scoped, conversationStorageKeys.v3ConflictPrefix)
    .map((key) => parseDurableConflictBranch(scoped.getItem(key)))
    .filter((value) => value !== null)
    .flatMap(({ writerSessionId: _writerSessionId, ...branch }) => {
      const checkpoint = verifiedCheckpoint(
        scoped.getItem(sessionSnapshotKeyV3(branch.conversationId, branch.branchRevision)),
        branch.conversationId,
        branch.branchRevision,
      );
      if (!checkpoint) throw new Error(`冲突 ${branch.conflictId} 的分支快照未通过摘要校验`);
      const { writerId: _writerId, ...portableCheckpoint } = checkpoint;
      return [{ branch, checkpoint: portableCheckpoint }];
    });
  const draftPrefix = epoch === "legacy"
    ? LEGACY_DRAFT_PREFIX
    : `${WORKSPACE_EPOCH_PREFIX}${epoch}.draft.`;
  const drafts = includeDrafts
    ? keys(session, draftPrefix).flatMap((key) => {
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

export async function applyCoordinatedWorkspaceRestore(
  restoreId: string,
  mode: RestoreMode,
  storage: StorageLike = window.localStorage,
  session: StorageLike = window.sessionStorage,
): Promise<{ imported: number; remapped: number }> {
  const ownerDocumentId = globalThis.crypto?.randomUUID?.() ?? `document-${Date.now()}`;
  const requestedTargetEpoch = `epoch-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
  const { fence } = beginWorkspaceRestoreFence(restoreId, requestedTargetEpoch, ownerDocumentId, storage);
  const targetEpoch = fence.targetEpoch;
  let frontendDigest: string | undefined;
  let serverCompleted = false;
  try {
    const prepared = await prepareWorkspaceRestore(restoreId, {
      mode,
      previousEpoch: fence.previousEpoch,
      targetEpoch,
      ownerDocumentId,
    });
    let imported = 0;
    let remapped = 0;
    if (prepared.frontend) {
      const staged = await stageRestoreEnvelope(
        prepared.frontend,
        {
          restoreId,
          targetEpoch,
          serverTransactionDigest: prepared.serverTransactionDigest ?? "",
        },
        storage,
        session,
      );
      imported = staged.stagedHeads;
      remapped = staged.remapped;
      frontendDigest = staged.digest;
      await markWorkspaceFrontendPrepared(restoreId, frontendDigest);
      await commitWorkspaceRestore(restoreId, { frontendCommitted: false, frontendDigest });
      activateRestoreEpoch(restoreId, storage);
      await commitWorkspaceRestore(restoreId, { frontendCommitted: true, frontendDigest });
      markServerCommitted(restoreId, storage);
    } else {
      await commitWorkspaceRestore(restoreId, { frontendCommitted: false });
    }
    await completeWorkspaceRestore(restoreId, frontendDigest);
    serverCompleted = true;
    completeFrontendRestore(restoreId, storage);
    return { imported, remapped };
  } catch (reason) {
    if (serverCompleted) throw reason;
    let rolledBack = false;
    try {
      const aborted = await abortWorkspaceRestore(restoreId);
      rolledBack = aborted.phase === "rolled-back";
    } catch {
      // Server may be unreachable.  Keep the fence and both journals; startup
      // recovery must query server state before selecting an epoch.
    }
    if (rolledBack) rollbackRestoreEpoch(restoreId, storage);
    throw reason;
  }
}

/** Resume an interrupted owner tab deterministically from both journals. */
export async function recoverInterruptedFrontendRestore(
  storage: StorageLike = window.localStorage,
): Promise<"none" | "complete" | "rolled-back" | "fenced"> {
  const journal = readFrontendRestoreJournal(storage);
  if (!journal) return "none";
  if (journal.phase === "complete") {
    // A renderer can stop after journaling completion but before deleting the
    // fence key.  Completion is already durable, so finishing this cleanup is
    // deterministic and does not require another server decision.
    storage.removeItem(WORKSPACE_RESTORE_FENCE_KEY);
    return "complete";
  }
  let server;
  try {
    server = await getWorkspaceRestore(journal.restoreId);
  } catch {
    return "fenced";
  }
  if (server.phase === "rolled-back" || server.phase === "failed") {
    rollbackRestoreEpoch(journal.restoreId, storage);
    return "rolled-back";
  }
  if (server.phase === "complete") {
    await verifyRestoreEpoch(journal.restoreId, storage);
    if (readActiveWorkspaceEpoch(storage) !== journal.targetEpoch) {
      storage.setItem(WORKSPACE_ACTIVE_EPOCH_KEY, journal.targetEpoch);
      if (storage.getItem(WORKSPACE_ACTIVE_EPOCH_KEY) !== journal.targetEpoch) return "fenced";
    }
    completeFrontendRestore(journal.restoreId, storage);
    return "complete";
  }
  const frontendDigest = await sha256(journal.entries);
  if (server.phase === "backend-committed") {
    if (readActiveWorkspaceEpoch(storage) !== journal.targetEpoch) {
      storage.setItem(WORKSPACE_ACTIVE_EPOCH_KEY, journal.targetEpoch);
      if (storage.getItem(WORKSPACE_ACTIVE_EPOCH_KEY) !== journal.targetEpoch) return "fenced";
    }
    markServerCommitted(journal.restoreId, storage);
    await completeWorkspaceRestore(journal.restoreId, journal.entries.length ? frontendDigest : undefined);
    completeFrontendRestore(journal.restoreId, storage);
    return "complete";
  }
  if (
    journal.phase === "staged"
    && ["backend-staged", "frontend-staged", "commit-intent"].includes(server.phase)
  ) {
    await verifyRestoreEpoch(journal.restoreId, storage);
    if (server.phase === "backend-staged") {
      await markWorkspaceFrontendPrepared(journal.restoreId, frontendDigest);
    }
    if (server.phase !== "commit-intent") {
      await commitWorkspaceRestore(journal.restoreId, { frontendCommitted: false, frontendDigest });
    }
    activateRestoreEpoch(journal.restoreId, storage);
    await commitWorkspaceRestore(journal.restoreId, { frontendCommitted: true, frontendDigest });
    markServerCommitted(journal.restoreId, storage);
    await completeWorkspaceRestore(journal.restoreId, frontendDigest);
    completeFrontendRestore(journal.restoreId, storage);
    return "complete";
  }
  if (server.phase === "commit-intent" && journal.phase === "active-epoch-switched") {
    await commitWorkspaceRestore(journal.restoreId, {
      frontendCommitted: true,
      frontendDigest,
    });
    markServerCommitted(journal.restoreId, storage);
    await completeWorkspaceRestore(journal.restoreId, frontendDigest);
    completeFrontendRestore(journal.restoreId, storage);
    return "complete";
  }
  return "fenced";
}

export function listenForRestoreEpoch(onChanged: (fence: WorkspaceRestoreFence) => void): () => void {
  let channel: BroadcastChannel | null = null;
  try {
    channel = new BroadcastChannel(RESTORE_CHANNEL);
    channel.onmessage = (event) => {
      if (event.data?.type === "workspace_restore_fence" && event.data.fence) onChanged(event.data.fence);
    };
  } catch {
    channel = null;
  }
  const onStorage = (event: StorageEvent) => {
    if (event.key === WORKSPACE_RESTORE_FENCE_KEY && event.newValue) {
      const fence = parseObject(event.newValue) as unknown as WorkspaceRestoreFence | null;
      if (fence?.restoreId) onChanged(fence);
      return;
    }
    if (event.key === WORKSPACE_ACTIVE_EPOCH_KEY && event.newValue) {
      onChanged({
        schemaVersion: 1,
        restoreId: "external-epoch-change",
        previousEpoch: "",
        targetEpoch: event.newValue,
        ownerDocumentId: "",
        phase: "commit-intent",
        createdAt: Date.now(),
        expiresAt: Number.MAX_SAFE_INTEGER,
      });
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    channel?.close();
    window.removeEventListener("storage", onStorage);
  };
}
