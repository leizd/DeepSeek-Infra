import { httpClient, type HttpClient } from "./httpClient";

export type WorkspacePackagePurpose = "share-export" | "restorable-backup";
export type BackupPhase = "preparing" | "quiescing" | "snapshotting" | "verifying" | "ready" | "failed";
export type RestoreMode = "merge" | "project-copy" | "replace-empty";

export interface BackupRequest {
  mode: "full" | "project";
  projectIds: string[];
  includeHistory: boolean;
  includeDrafts: boolean;
  includeRebuildableIndexes: boolean;
}

export interface BackupSession {
  backupId: string;
  phase: BackupPhase;
  requiresFrontendState: boolean;
  estimatedBytes: number;
  included: string[];
  excluded: string[];
  filename?: string;
  downloadUrl?: string;
}

export interface RestorePlan {
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
}

export interface RestoreResult {
  restoreId: string;
  phase: "complete" | "ready-for-frontend";
  restoreEpoch: number;
  safetyBackupId: string;
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
    encrypted: false;
    integrityVerified: true;
    includedByDefault: string[];
    alwaysExcluded: string[];
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

export async function inspectBackup(file: File, client: HttpClient = httpClient): Promise<RestorePlan> {
  const form = new FormData();
  form.append("file", file, file.name);
  const response = await client.request("/api/workspace/restores/inspect", { method: "POST", body: form });
  return response.json() as Promise<RestorePlan>;
}

export function applyWorkspaceRestore(restoreId: string, mode: RestoreMode, client: HttpClient = httpClient) {
  return client.postJson<RestoreResult>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/apply`, { mode });
}
