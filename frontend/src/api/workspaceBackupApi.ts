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

export interface BackupMirrorRecipientVariantV1 {
  recipientSetDigest: string;
  ciphertextSha256: string;
  filename: string;
  creationVerified: boolean;
}

export interface BackupMirrorMetadataV1 {
  schemaVersion: 1 | 2;
  profileId: string;
  generationId?: string;
  parentGenerationId?: string | null;
  sourceEpoch: string;
  clientReplicaId?: string;
  clientSequence?: number;
  envelopeDigest: string;
  recipientSetDigest: string;
  recipientVariants?: BackupMirrorRecipientVariantV1[];
  conversations: number;
  conflicts: number;
  createdAt: string;
  acknowledgedAt: string;
  ciphertextSha256: string;
  creationVerified: boolean;
  idempotent?: boolean;
}

export type BackupMirrorFreshness = "current" | "stale" | "missing" | "epoch-mismatch" | "recipient-mismatch" | "excluded";

export interface BackupMirrorStatus {
  status: BackupMirrorFreshness;
  mirror?: BackupMirrorMetadataV1;
  profileId?: string;
}

export interface PutBackupMirrorRequest {
  sourceEpoch: string;
  acknowledgedAt?: string;
  envelope: FrontendBackupEnvelopeV1;
  clientReplicaId?: string;
  clientSequence?: number;
  expectedHeadGenerationId?: string;
}

export function listBackupMirrors(client: HttpClient = httpClient) {
  return client.json<{ mirrors: BackupMirrorMetadataV1[] }>("/api/workspace/backup-mirrors");
}

export function getBackupMirror(profileId: string, client: HttpClient = httpClient) {
  return client.json<BackupMirrorStatus>(`/api/workspace/backup-mirrors/${encodeURIComponent(profileId)}`);
}

export function putBackupMirror(
  profileId: string,
  request: PutBackupMirrorRequest,
  client: HttpClient = httpClient,
) {
  return client.json<BackupMirrorMetadataV1>(`/api/workspace/backup-mirrors/${encodeURIComponent(profileId)}/frontend`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
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

export interface BackupPolicyV1 {
  schemaVersion: 1;
  policyId: string;
  name: string;
  enabled: boolean;
  schedule: {
    cron: string;
    timezone: string;
    misfirePolicy: "skip" | "run-once";
    catchupWindowSeconds: number;
    jitterSeconds: number;
  };
  scope: {
    mode: "full" | "project";
    projectIds: string[];
    includeHistory: boolean;
    includeExternalState: boolean;
    coveragePolicy: "strict" | "best-effort";
  };
  frontendMirror: { mode: "required" | "best-effort" | "excluded"; profileId?: string; maxAgeSeconds: number };
  protection: { mode: "age-recipient"; recipients: string[] };
  targetId: string;
  retentionPolicyId: string;
  retry: { maxAttempts: number; initialBackoffSeconds: number; maxBackoffSeconds: number };
  createdAt: string;
  updatedAt: string;
}

export interface BackupNextRun {
  scheduledFor: string;
  localDateTime: string;
  timezone: string;
  slotKey: string;
  jitterSeconds: number;
}

export interface BackupRunRecord {
  runId: string;
  policyId: string;
  scheduleSlot: string;
  phase: string;
  attempt: number;
  reason?: string | null;
  error?: string | null;
  backupId?: string | null;
  filename?: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface BackupTargetRecord {
  schemaVersion: number;
  targetId: string;
  kind?: "filesystem" | "s3" | "webdav";
  path?: string;
  bucket?: string;
  prefix?: string;
  region?: string | null;
  endpointUrl?: string | null;
  credentialProvider?: { type: string; profile?: string };
  label: string;
  createdAt: string;
  lastProbe?: {
    status?: string;
    scheduledBackupReady?: boolean;
    results?: Record<string, string>;
    capabilities?: Record<string, unknown>;
  };
}

export interface BackupTargetHealth {
  targetId: string;
  status: string;
  checkedAt: string;
  detail?: string | null;
}

export interface BackupCatalogEntry {
  backupId: string;
  policyId: string;
  targetId: string;
  scheduleSlot: string;
  filename: string;
  size: number;
  createdAt: string;
  creationVerified: boolean;
  pinned: boolean;
  ciphertextScrubbedAt?: string | null;
  scrubOk?: boolean | null;
  userUnlockVerifiedAt?: string | null;
  trashed?: boolean;
}

export interface BackupCatalogView {
  backups: BackupCatalogEntry[];
  chainValid: boolean;
  integrity: { orphans: string[]; missing: string[] };
  health: {
    status: string;
    backups: Array<{
      backupId: string;
      status: string;
      issues: string[];
      creationVerified: boolean;
      ciphertextScrubbedAt?: string | null;
      userUnlockVerifiedAt?: string | null;
    }>;
  };
}

export function listBackupPolicies(client: HttpClient = httpClient) {
  return client.json<{ policies: BackupPolicyV1[]; nextRuns: Record<string, BackupNextRun | null> }>("/api/workspace/backup-policies");
}

export function createBackupPolicy(request: Partial<BackupPolicyV1>, client: HttpClient = httpClient) {
  return client.postJson<BackupPolicyV1>("/api/workspace/backup-policies", request);
}

export function updateBackupPolicy(policyId: string, patch: Partial<BackupPolicyV1>, client: HttpClient = httpClient) {
  return client.json<BackupPolicyV1>(`/api/workspace/backup-policies/${encodeURIComponent(policyId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function deleteBackupPolicy(policyId: string, client: HttpClient = httpClient) {
  return client.json<{ deleted: boolean; policyId: string }>(`/api/workspace/backup-policies/${encodeURIComponent(policyId)}`, { method: "DELETE" });
}

export function runBackupPolicy(policyId: string, client: HttpClient = httpClient) {
  return client.postJson<{ phase: string; backupId?: string; filename?: string; error?: string; reason?: string }>(
    `/api/workspace/backup-policies/${encodeURIComponent(policyId)}/run`,
    {},
  );
}

export function listBackupTargets(client: HttpClient = httpClient) {
  return client.json<{ targets: BackupTargetRecord[]; health: BackupTargetHealth[] }>("/api/workspace/backup-targets");
}

export function createBackupTarget(
  request:
    | { kind?: "filesystem"; path: string; label?: string }
    | {
        kind: "s3" | "s3-compatible";
        bucket: string;
        prefix?: string;
        region?: string;
        endpointUrl?: string;
        expectedBucketOwner?: string;
        label?: string;
        credentialProvider?: { type: string; profile?: string };
        probe?: boolean;
      },
  client: HttpClient = httpClient,
) {
  return client.postJson<BackupTargetRecord>("/api/workspace/backup-targets", request);
}

export function probeBackupTarget(targetId: string, client: HttpClient = httpClient) {
  return client.postJson<{
    targetId: string;
    ready: boolean;
    status: string;
    detail?: string;
    kind?: string;
    scheduledBackupReady?: boolean;
    probe?: BackupTargetRecord["lastProbe"];
  }>(`/api/workspace/backup-targets/${encodeURIComponent(targetId)}/probe`, {});
}

export function listBackupTargetCapabilities(client: HttpClient = httpClient) {
  return client.json<{
    s3TargetAvailable: boolean;
    webdavTargetAvailable: boolean;
    supportedKinds: string[];
    reservedKinds: string[];
  }>("/api/workspace/backup-target-capabilities");
}

export function restoreBackupFromTarget(
  request: { targetId: string; backupId: string; complete?: boolean },
  client: HttpClient = httpClient,
) {
  return client.postJson<{
    restoreId: string;
    phase?: string;
    targetId?: string;
    backupId?: string;
    filename?: string;
    size?: number;
    objectDigest?: string;
    path?: string;
    downloadedBytes?: number;
    expectedBytes?: number;
  }>("/api/workspace/restores/from-target", request);
}

export function fetchRemoteRestore(restoreId: string, request: { maxBytes?: number } = {}, client: HttpClient = httpClient) {
  return client.postJson<{
    restoreId: string;
    phase: string;
    downloadedBytes: number;
    expectedBytes: number;
    path?: string;
    next?: string;
  }>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/fetch`, request);
}

export function materializeRemoteRestore(
  restoreId: string,
  request: { mode?: "merge" | "project-copy" | "replace-empty"; previousEpoch?: string; targetEpoch?: string; ownerDocumentId?: string },
  client: HttpClient = httpClient,
) {
  return client.postJson<{
    restoreId: string;
    phase: "prepared";
    remoteRestorePhase: "prepared";
    materializedTreeVerified: boolean;
    serverTransactionDigest?: string;
    frontend?: unknown;
  }>(`/api/workspace/restores/${encodeURIComponent(restoreId)}/materialize`, request);
}

export function deleteBackupTarget(targetId: string, client: HttpClient = httpClient) {
  return client.json<{ deleted: boolean; targetId: string }>(`/api/workspace/backup-targets/${encodeURIComponent(targetId)}`, { method: "DELETE" });
}

export function listBackupRuns(policyId?: string, client: HttpClient = httpClient) {
  const query = policyId ? `?policyId=${encodeURIComponent(policyId)}` : "";
  return client.json<{ runs: BackupRunRecord[] }>(`/api/workspace/backup-runs${query}`);
}

export function getBackupCatalog(targetId?: string, client: HttpClient = httpClient) {
  const query = targetId ? `?targetId=${encodeURIComponent(targetId)}` : "";
  return client.json<BackupCatalogView>(`/api/workspace/backup-catalog${query}`);
}

export function pinCatalogBackup(backupId: string, pinned: boolean, client: HttpClient = httpClient) {
  return client.json<{ backupId: string; pinned: boolean }>(`/api/workspace/backup-catalog/${encodeURIComponent(backupId)}/pin`, {
    method: pinned ? "POST" : "DELETE",
  });
}

export function previewBackupRetention(policyId: string, client: HttpClient = httpClient) {
  return client.postJson<{ keep: string[]; trash: string[]; protected: Array<{ backupId: string; reason: string }> }>(
    "/api/workspace/retention/preview",
    { policyId },
  );
}

export function applyBackupRetention(policyId: string, client: HttpClient = httpClient) {
  return client.postJson<{ applied: { trashed: string[] }; finalized: { deleted: string[] } }>("/api/workspace/retention/apply", { policyId });
}

export function scrubCatalogBackup(backupId: string, client: HttpClient = httpClient) {
  return client.postJson<{ backupId: string; ok: boolean; checks: Record<string, string>; scrubbedAt: string }>(
    `/api/workspace/backups/${encodeURIComponent(backupId)}/scrub`,
    {},
  );
}

export function verifyUnlockCatalogBackup(backupId: string, identity: string, client: HttpClient = httpClient) {
  return client.postJson<{ backupId: string; ok: boolean; userUnlockVerifiedAt: string; sealedFrontend?: { sourceEpoch?: string } | null }>(
    `/api/workspace/backups/${encodeURIComponent(backupId)}/verify-unlock`,
    { identity },
  );
}
