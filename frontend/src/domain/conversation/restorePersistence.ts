import type { FrontendBackupEnvelopeV1 } from "../../api/workspaceBackupApi";
import type { ComposerDraft } from "../../features/composer/composerDraftPersistence";
import {
  checkpointDigest,
  conversationStorageKeys,
  createConversationPersistenceAdapter,
  DEFAULT_WORKSPACE_EPOCH,
  readActiveWorkspaceEpoch,
  sessionConflictIndexKeyV3,
  sessionConflictKeyV3,
  sessionHeadKeyV3,
  sessionSnapshotKeyV3,
  WORKSPACE_ACTIVE_EPOCH_KEY,
  WORKSPACE_EPOCH_PREFIX,
  WORKSPACE_RESTORE_FENCE_KEY,
  workspaceEpochStorage,
  type ConversationCheckpointV3,
  type DurableConflictBranch,
  type StorageLike,
} from "./persistence";
import type { Conversation } from "./types";

export const FRONTEND_RESTORE_JOURNAL_KEY = "deepseek-infra.workspace.restore-journal";
export const RESTORE_CHANNEL = "deepseek-infra-workspace-restore";

export interface WorkspaceRestoreFence {
  schemaVersion: 1;
  restoreId: string;
  previousEpoch: string;
  targetEpoch: string;
  ownerDocumentId: string;
  phase: "preparing" | "frontend-staged" | "commit-intent" | "backend-committed" | "complete" | "aborting";
  createdAt: number;
  expiresAt: number;
}

export interface FrontendRestoreJournalV1 {
  schemaVersion: 1;
  restoreId: string;
  previousEpoch: string;
  targetEpoch: string;
  restoreWriterId: string;
  envelopeDigest: string;
  serverTransactionDigest: string;
  phase: "staging" | "staged" | "active-epoch-switched" | "server-committed" | "complete" | "rolling-back";
  entries: Array<{ key: string; digest: string }>;
  createdAt: number;
  updatedAt: number;
}

export interface ReplicaImportResult {
  stagedHeads: number;
  stagedConflicts: number;
  stagedDrafts: number;
  digest: string;
  remapped: number;
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
  const bytes = new TextEncoder().encode(typeof value === "string" ? value : JSON.stringify(stableValue(value)));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, "0")).join("");
}

function randomId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `restore-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function readJournal(storage: StorageLike): FrontendRestoreJournalV1 | null {
  const value = parseObject(storage.getItem(FRONTEND_RESTORE_JOURNAL_KEY)) as Partial<FrontendRestoreJournalV1> | null;
  return value?.schemaVersion === 1 && typeof value.restoreId === "string"
    ? value as FrontendRestoreJournalV1
    : null;
}

function verifiedSet(storage: StorageLike, key: string, value: string): void {
  storage.setItem(key, value);
  if (storage.getItem(key) !== value) throw new Error(`恢复暂存键 ${key} 写入后回读核验失败`);
}

function writeJournal(storage: StorageLike, journal: FrontendRestoreJournalV1): void {
  const serialized = JSON.stringify(journal);
  verifiedSet(storage, FRONTEND_RESTORE_JOURNAL_KEY, serialized);
}

export function beginWorkspaceRestoreFence(
  restoreId: string,
  targetEpoch: string,
  ownerDocumentId: string,
  storage: StorageLike = window.localStorage,
): { fence: WorkspaceRestoreFence; journal: FrontendRestoreJournalV1 } {
  const previousEpoch = readActiveWorkspaceEpoch(storage);
  const existing = readJournal(storage);
  const effectivePreviousEpoch = existing?.restoreId === restoreId ? existing.previousEpoch : previousEpoch;
  const effectiveTargetEpoch = existing?.restoreId === restoreId ? existing.targetEpoch : targetEpoch;
  const now = Date.now();
  const fence: WorkspaceRestoreFence = {
    schemaVersion: 1,
    restoreId,
    previousEpoch: effectivePreviousEpoch,
    targetEpoch: effectiveTargetEpoch,
    ownerDocumentId,
    phase: "preparing",
    createdAt: now,
    expiresAt: now + 24 * 60 * 60 * 1000,
  };
  const journal: FrontendRestoreJournalV1 = existing?.restoreId === restoreId
    ? { ...existing, updatedAt: now }
    : {
        schemaVersion: 1,
        restoreId,
        previousEpoch: effectivePreviousEpoch,
        targetEpoch: effectiveTargetEpoch,
        restoreWriterId: randomId(),
        envelopeDigest: "",
        serverTransactionDigest: "",
        phase: "staging",
        entries: [],
        createdAt: now,
        updatedAt: now,
      };
  verifiedSet(storage, WORKSPACE_RESTORE_FENCE_KEY, JSON.stringify(fence));
  writeJournal(storage, journal);
  return { fence, journal };
}

function updateFence(storage: StorageLike, phase: WorkspaceRestoreFence["phase"]): void {
  const value = parseObject(storage.getItem(WORKSPACE_RESTORE_FENCE_KEY)) as unknown as WorkspaceRestoreFence | null;
  if (!value) throw new Error("浏览器恢复围栏丢失");
  verifiedSet(storage, WORKSPACE_RESTORE_FENCE_KEY, JSON.stringify({ ...value, phase }));
}

function clearTargetEpoch(storage: StorageLike, targetEpoch: string): void {
  if (typeof storage.length !== "number" || typeof storage.key !== "function") return;
  const prefix = `${WORKSPACE_EPOCH_PREFIX}${targetEpoch}.`;
  const targets: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key?.startsWith(prefix)) targets.push(key);
  }
  targets.forEach((key) => storage.removeItem(key));
}

function restoredConversation(
  checkpoint: Partial<ConversationCheckpointV3>,
  conversationId: string,
): Conversation {
  if (!checkpoint.conversation || checkpointDigest(JSON.stringify(checkpoint.conversation)) !== checkpoint.digest) {
    throw new Error(`会话 ${conversationId} 的恢复快照无效`);
  }
  return structuredClone(checkpoint.conversation);
}

export async function stageRestoreEnvelope(
  envelope: FrontendBackupEnvelopeV1,
  options: {
    restoreId: string;
    targetEpoch: string;
    serverTransactionDigest: string;
  },
  storage: StorageLike = window.localStorage,
  session: StorageLike = window.sessionStorage,
): Promise<ReplicaImportResult> {
  const journal = readJournal(storage);
  if (!journal || journal.restoreId !== options.restoreId || journal.targetEpoch !== options.targetEpoch) {
    throw new Error("浏览器恢复 Journal 与服务端事务不匹配");
  }
  clearTargetEpoch(storage, options.targetEpoch);
  clearTargetEpoch(session, options.targetEpoch);
  const target = workspaceEpochStorage(storage, options.targetEpoch);
  const persistence = createConversationPersistenceAdapter();
  const existing = persistence.load(storage, session);
  const conversations = [...existing.conversations];
  const remappedIds = new Map<string, string>();
  let remapped = 0;
  for (const entry of envelope.conversations) {
    const checkpoint = entry.checkpoint as Partial<ConversationCheckpointV3>;
    const conversation = restoredConversation(checkpoint, entry.conversationId);
    const previous = conversations.find((item) => item.id === entry.conversationId);
    if (previous && checkpointDigest(JSON.stringify(previous)) === checkpoint.digest) continue;
    if (previous) {
      conversation.id = `${entry.conversationId}.imported.${checkpointDigest(`${options.restoreId}\0${entry.conversationId}`).slice(0, 12)}`;
      conversation.title = `${conversation.title}（恢复副本）`;
      remappedIds.set(entry.conversationId, conversation.id);
      remapped += 1;
    }
    conversations.push(conversation);
  }

  const entries: Array<{ key: string; digest: string }> = [];
  const record = async (key: string, value: unknown, targetStorage: StorageLike = target): Promise<void> => {
    const serialized = JSON.stringify(value);
    verifiedSet(targetStorage, key, serialized);
    entries.push({ key, digest: await sha256(serialized) });
  };
  const headRevision = new Map<string, string>();
  let sequence = 0;
  for (const conversation of conversations) {
    sequence += 1;
    const revision = `${sequence}.${journal.restoreWriterId}`;
    const digest = checkpointDigest(JSON.stringify(conversation));
    const savedAt = Date.now();
    const checkpoint: ConversationCheckpointV3 = {
      schemaVersion: 3,
      conversationId: conversation.id,
      revision,
      parentRevision: null,
      writerId: journal.restoreWriterId,
      savedAt,
      digest,
      conversation,
    };
    await record(sessionSnapshotKeyV3(conversation.id, revision), checkpoint);
    await record(sessionHeadKeyV3(conversation.id), {
      revision,
      parentRevision: null,
      writerId: journal.restoreWriterId,
      savedAt,
      digest,
    });
    headRevision.set(conversation.id, revision);
  }

  const importedConflicts: Array<{ branch: DurableConflictBranch; conversation: Conversation }> = [];
  for (const branch of persistence.listConflictBranches(undefined, storage)) {
    const loaded = persistence.readConflictBranch(branch.conversationId, storage, branch.conflictId);
    if (loaded) importedConflicts.push({ branch, conversation: loaded.conversation });
  }
  for (const raw of envelope.conflicts) {
    if (!raw || typeof raw !== "object") continue;
    const source = raw as { branch?: Partial<DurableConflictBranch>; checkpoint?: Partial<ConversationCheckpointV3> };
    if (!source.branch?.conversationId || !source.branch.conflictId || !source.checkpoint?.conversation) continue;
    importedConflicts.push({
      branch: source.branch as DurableConflictBranch,
      conversation: restoredConversation(source.checkpoint, source.branch.conversationId),
    });
  }
  const conflictIdsByConversation = new Map<string, string[]>();
  for (const imported of importedConflicts) {
    const originalId = imported.branch.conversationId;
    const conversationId = remappedIds.get(originalId) ?? originalId;
    const conversation = { ...imported.conversation, id: conversationId };
    sequence += 1;
    const revision = `${sequence}.${journal.restoreWriterId}`;
    const conflictId = checkpointDigest(
      `${options.restoreId}\0${originalId}\0${imported.branch.conflictId}\0${conversationId}`,
    );
    const digest = checkpointDigest(JSON.stringify(conversation));
    const savedAt = Date.now();
    await record(sessionSnapshotKeyV3(conversationId, revision), {
      schemaVersion: 3,
      conversationId,
      revision,
      parentRevision: null,
      writerId: journal.restoreWriterId,
      savedAt,
      digest,
      conversation,
    } satisfies ConversationCheckpointV3);
    const sharedRevision = headRevision.get(conversationId) ?? revision;
    const branch: DurableConflictBranch = {
      schemaVersion: 1,
      conflictId,
      conversationId,
      branchRevision: revision,
      parentBranchRevision: null,
      baseRevision: sharedRevision,
      sharedRevision,
      writerSessionId: journal.restoreWriterId,
      createdAt: savedAt,
      updatedAt: savedAt,
      status: "pending",
    };
    await record(sessionConflictKeyV3(conversationId, conflictId), branch);
    await record(sessionConflictKeyV3(conversationId), {
      revision,
      baseRevision: sharedRevision,
      sharedRevision,
      writerId: journal.restoreWriterId,
      savedAt,
    });
    const ids = conflictIdsByConversation.get(conversationId) ?? [];
    if (!ids.includes(conflictId)) ids.push(conflictId);
    conflictIdsByConversation.set(conversationId, ids);
  }
  for (const [conversationId, conflictIds] of conflictIdsByConversation) {
    await record(sessionConflictIndexKeyV3(conversationId), { schemaVersion: 1, conflictIds });
  }

  let stagedDrafts = 0;
  for (const raw of envelope.drafts ?? []) {
    if (!raw || typeof raw !== "object") continue;
    const draft = raw as Partial<ComposerDraft>;
    if (typeof draft.conversationId !== "string" || typeof draft.text !== "string" || typeof draft.updatedAt !== "number") continue;
    const conversationId = remappedIds.get(draft.conversationId) ?? draft.conversationId;
    const key = `${WORKSPACE_EPOCH_PREFIX}${options.targetEpoch}.draft.${encodeURIComponent(conversationId)}:${encodeURIComponent(
      typeof draft.projectId === "string" ? draft.projectId : "",
    )}`;
    await record(key, { ...draft, conversationId, projectId: typeof draft.projectId === "string" ? draft.projectId : null }, session);
    stagedDrafts += 1;
  }

  const digest = await sha256(entries);
  const staged: FrontendRestoreJournalV1 = {
    ...journal,
    envelopeDigest: envelope.digest,
    serverTransactionDigest: options.serverTransactionDigest,
    phase: "staged",
    entries,
    updatedAt: Date.now(),
  };
  writeJournal(storage, staged);
  updateFence(storage, "frontend-staged");
  await verifyRestoreEpoch(options.restoreId, storage, session);
  return {
    stagedHeads: conversations.length,
    stagedConflicts: importedConflicts.length,
    stagedDrafts,
    digest,
    remapped,
  };
}

export async function verifyRestoreEpoch(
  restoreId: string,
  storage: StorageLike = window.localStorage,
  session: StorageLike = window.sessionStorage,
): Promise<string> {
  const journal = readJournal(storage);
  if (!journal || journal.restoreId !== restoreId) throw new Error("浏览器恢复 Journal 不存在");
  const target = workspaceEpochStorage(storage, journal.targetEpoch);
  for (const entry of journal.entries) {
    const targetStorage = entry.key.startsWith(`${WORKSPACE_EPOCH_PREFIX}${journal.targetEpoch}.draft.`) ? session : target;
    const value = targetStorage.getItem(entry.key);
    if (value === null || await sha256(value) !== entry.digest) {
      throw new Error(`恢复暂存键 ${entry.key} 未通过摘要核验`);
    }
  }
  return sha256(journal.entries);
}

function broadcast(fence: WorkspaceRestoreFence): void {
  try {
    new BroadcastChannel(RESTORE_CHANNEL).postMessage({ type: "workspace_restore_fence", fence });
  } catch {
    // localStorage events are the fallback.
  }
}

export function activateRestoreEpoch(
  restoreId: string,
  storage: StorageLike = window.localStorage,
): FrontendRestoreJournalV1 {
  const journal = readJournal(storage);
  if (!journal || journal.restoreId !== restoreId || journal.phase !== "staged") {
    throw new Error("浏览器恢复尚未完成暂存");
  }
  verifiedSet(storage, WORKSPACE_ACTIVE_EPOCH_KEY, journal.targetEpoch);
  const next = { ...journal, phase: "active-epoch-switched" as const, updatedAt: Date.now() };
  writeJournal(storage, next);
  updateFence(storage, "commit-intent");
  const fence = parseObject(storage.getItem(WORKSPACE_RESTORE_FENCE_KEY)) as unknown as WorkspaceRestoreFence;
  broadcast(fence);
  return next;
}

export function markServerCommitted(restoreId: string, storage: StorageLike = window.localStorage): void {
  const journal = readJournal(storage);
  if (!journal || journal.restoreId !== restoreId) throw new Error("浏览器恢复 Journal 不存在");
  writeJournal(storage, { ...journal, phase: "server-committed", updatedAt: Date.now() });
  updateFence(storage, "backend-committed");
}

export function completeFrontendRestore(restoreId: string, storage: StorageLike = window.localStorage): void {
  const journal = readJournal(storage);
  if (!journal || journal.restoreId !== restoreId) throw new Error("浏览器恢复 Journal 不存在");
  writeJournal(storage, { ...journal, phase: "complete", updatedAt: Date.now() });
  updateFence(storage, "complete");
  storage.removeItem(WORKSPACE_RESTORE_FENCE_KEY);
}

export function rollbackRestoreEpoch(restoreId: string, storage: StorageLike = window.localStorage): void {
  const journal = readJournal(storage);
  if (!journal || journal.restoreId !== restoreId) return;
  writeJournal(storage, { ...journal, phase: "rolling-back", updatedAt: Date.now() });
  verifiedSet(storage, WORKSPACE_ACTIVE_EPOCH_KEY, journal.previousEpoch || DEFAULT_WORKSPACE_EPOCH);
  storage.removeItem(WORKSPACE_RESTORE_FENCE_KEY);
}

export function readFrontendRestoreJournal(storage: StorageLike = window.localStorage): FrontendRestoreJournalV1 | null {
  return readJournal(storage);
}
