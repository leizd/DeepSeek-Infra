import { httpClient, type HttpClient } from "./httpClient";

export type WorkspacePackagePurpose = "share-export" | "restorable-backup";
export type BackupPhase = "preparing" | "quiescing" | "snapshotting" | "verifying" | "ready" | "failed";
export type BackupProtection =
  | { mode: "none" }
  | { mode: "passphrase" }
  | { mode: "age-recipient"; recipients: string[] };
export type CoveragePolicy = "strict" | "best-effort";
export type RestoreMode = "merge" | "project-copy" | "replace-empty";
export type RestoreSecretState = "not-required" | "available" | "expired" | "required-for-safety-backup";
export type RestorePhase =
  | "inspected"
  | "preparing"
  | "backend-staged"
  | "frontend-staged"
  | "commit-intent"
  | "frontend-committed"
  | "backend-committed"
  | "complete"
  | "aborting"
  | "rolled-back"
  | "recovery-required"
  | "failed";

export interface BackupRequest {
  mode: "full" | "project";
  projectIds: string[];
  includeHistory: boolean;
  includeDrafts: boolean;
  includeRebuildableIndexes: boolean;
  includeExternalState: boolean;
  coveragePolicy: CoveragePolicy;
  protection: BackupProtection;
}

export interface BackupSession {
  backupId: string;
  phase: BackupPhase;
  requiresFrontendState: boolean;
  estimatedBytes: number;
  included: string[];
  excluded: string[];
  protection?: { mode: BackupProtection["mode"] };
  coverage?: BackupCoverage;
  filename?: string;
  downloadUrl?: string;
}

export interface RestorePlan {
  phase: "inspected";
  restoreId: string;
  sourceVersion: string;
  targetVersion: string;
  compatible: boolean;
  purpose: "restorable-backup";
  operations: Array<{ contributorId: string; files: number; conflicts: number }>;
  conflicts: Array<{ contributorId: string; count: number; strategy: string }>;
  migrations: unknown[];
  warnings: string[];
  estimatedWriteBytes: number;
  requiresFrontendApply: boolean;
  encrypted?: boolean;
  protection?: "none" | "passphrase" | "age-recipient";
  coverage?: BackupCoverage;
  secretState?: RestoreSecretState;
}

export interface LockedRestoreUpload {
  restoreId: string;
  phase: "locked";
  protection: "passphrase" | "age-recipient";
  ciphertextSha256: string;
}

export interface BackupCoverage {
  policy: CoveragePolicy;
  localContributors: string[];
  externalContributors: Array<{ id: string; status: string; schemaVersion?: number }>;
  unavailableDurableSources: Array<{ id: string; reason: string }>;
  complete: boolean;
}

export interface RestoreResult {
  restoreId: string;
  phase: RestorePhase | "ready-for-frontend";
  previousEpoch?: string;
  targetEpoch?: string;
  frontendDigest?: string;
  serverTransactionDigest?: string;
  safetyBackupId?: string;
  requiresFrontendApply?: boolean;
  frontend?: FrontendBackupEnvelopeV1 | null;
}

export interface FrontendBackupEnvelopeV1 {
  schemaVersion: 1;
  sourceVersion: string;
  createdAt: number;
  conversations: Array<{
    conversationId: string;
    headRevision: string;
    checkpoint: unknown;
  }>;
  conflicts: unknown[];
  drafts?: unknown[];
  digest: string;
}

export function backupCapabilities(client: HttpClient = httpClient) {
  return client.json<{
    purpose: "restorable-backup";
    encrypted: boolean;
    encryptedBackupAvailable: boolean;
    reason?: string;
    protectionModes: Array<BackupProtection["mode"]>;
    integrityVerified: true;
    includedByDefault: string[];
    alwaysExcluded: string[];
    externalContributors: Array<{ id: string; available: boolean; reason?: string }>;
  }>("/api/workspace/backups/capabilities");
}

export function createBackupSession(request: BackupRequest, client: HttpClient = httpClient) {
  return client.postJson<BackupSession>("/api/workspace/backups", {
    ...request,
    requiresFrontendState: true,
  });
}

export function uploadFrontendBackupState(
  backupId: string,
  envelope: FrontendBackupEnvelopeV1,
  client: HttpClient = httpClient,
) {
  return client.json<{ ok: true; digest: string }>(`/api/workspace/backups/${encodeURIComponent(backupId)}/frontend-state`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(envelope),
  });
}

export function finalizeBackup(backupId: string, client: HttpClient = httpClient) {
  return client.postJson<BackupSession>(`/api/workspace/backups/${encodeURIComponent(backupId)}/finalize`, {});
}

export function putBackupSecret(
  backupId: string,
  request: { kind: "passphrase" | "age-identity"; secret: string },
  client: HttpClient = httpClient,
) {
  return client.json<{ ok: true; expiresInSeconds: number; attemptsRemaining: number }>(
    `/api/workspace/backups/${encodeURIComponent(backupId)}/secret`,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) },
  );
}

export function generateRecoveryIdentity(client: HttpClient = httpClient) {
  return client.postJson<{ identity: string; recipient: string; displayedOnce: true }>(
    "/api/workspace/backups/recovery-identities",
    {},
  );
}

export async function inspectBackup(file: File, client: HttpClient = httpClient): Promise<RestorePlan | LockedRestoreUpload> {
  const response = await client.request("/api/workspace/restores/inspect", {
    method: "POST",
    headers: {
      "Content-Type": "application/vnd.deepseek-infra.backup+zip",
      "X-Backup-Filename": file.name,
    },
    body: file,
  });
  return response.json() as Promise<RestorePlan>;
}

export function putRestoreSecret(
  restoreId: string,
  request: { kind: "passphrase" | "age-identity"; secret: string },
  client: HttpClient = httpClient,
) {
  return client.json<{ ok: true; expiresInSeconds: number; attemptsRemaining: number }>(
    `/api/workspace/restores/${encodeURIComponent(restoreId)}/secret`,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) },
  );
}

export function unlockBackup(restoreId: string, client: HttpClient = httpClient) {
  return client.postJson<RestorePlan>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/unlock`, {});
}

export function applyWorkspaceRestore(restoreId: string, mode: RestoreMode, client: HttpClient = httpClient) {
  return client.postJson<RestoreResult>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/apply`, { mode });
}

export function prepareWorkspaceRestore(
  restoreId: string,
  request: { mode: RestoreMode; previousEpoch: string; targetEpoch: string; ownerDocumentId: string },
  client: HttpClient = httpClient,
) {
  return client.postJson<RestoreResult>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/prepare`, request);
}

export function markWorkspaceFrontendPrepared(
  restoreId: string,
  digest: string,
  client: HttpClient = httpClient,
) {
  return client.json<RestoreResult>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/frontend-prepared`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ digest }),
  });
}

export function commitWorkspaceRestore(
  restoreId: string,
  request: { frontendCommitted: boolean; frontendDigest?: string },
  client: HttpClient = httpClient,
) {
  return client.postJson<RestoreResult>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/commit`, request);
}

export function completeWorkspaceRestore(restoreId: string, frontendDigest?: string, client: HttpClient = httpClient) {
  return client.postJson<RestoreResult>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/complete`, {
    frontendDigest,
  });
}

export function abortWorkspaceRestore(restoreId: string, client: HttpClient = httpClient) {
  return client.postJson<RestoreResult>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/abort`, {});
}

export function getWorkspaceRestore(restoreId: string, client: HttpClient = httpClient) {
  return client.json<RestoreResult>(`/api/workspace/restores/${encodeURIComponent(restoreId)}`);
}
