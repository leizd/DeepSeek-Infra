import { createHash, randomUUID } from "node:crypto";

export type TaskStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";

export interface TestTaskArguments {
  target: string;
  keyword?: string;
  markers?: string;
  timeoutSeconds: number;
}

export interface TaskRecord {
  id: string;
  kind: "test-run";
  idempotencyKeyHash: string;
  requestHash: string;
  arguments: TestTaskArguments;
  status: TaskStatus;
  ownerInstance: string | null;
  leaseUntil: number | null;
  attempts: number;
  stdout: string;
  stderr: string;
  exitCode: number | null;
  error: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface CreateTaskInput {
  idempotencyKey: string;
  arguments: TestTaskArguments;
  now: number;
}

export interface CreateTaskResult {
  task: TaskRecord;
  deduplicated: boolean;
}

export interface TaskOutcome {
  stdout: string;
  stderr: string;
  exitCode: number | null;
  error: string | null;
}

export interface TaskStore {
  createOrGet(input: CreateTaskInput): Promise<CreateTaskResult>;
  get(taskId: string): Promise<TaskRecord | null>;
  claim(instanceId: string, now: number, leaseMs: number): Promise<TaskRecord | null>;
  heartbeat(taskId: string, instanceId: string, now: number, leaseMs: number): Promise<boolean>;
  complete(taskId: string, instanceId: string, outcome: TaskOutcome, now: number): Promise<TaskRecord | null>;
  backupCapabilities(): Promise<BackupCapabilities>;
  prepareBackup(backupId: string, now: number): Promise<BackupFence>;
  exportBackup(backupId: string): Promise<string>;
  releaseBackup(backupId: string): Promise<void>;
  restoreBackup(restoreId: string, snapshot: string, now: number): Promise<RestoreImportResult>;
  close(): Promise<void>;
}

export interface BackupCapabilities {
  contributorId: "stateless-mcp";
  schemaVersion: 1;
  dataClass: "durable";
  available: boolean;
}

export interface BackupFence {
  backupId: string;
  generation: number;
  createdAt: number;
  expiresAt: number;
}

export const BACKUP_FENCE_TTL_MS = 60 * 60 * 1000;

export interface RestoreImportResult {
  restoreId: string;
  restoreEpoch: string;
  imported: number;
  skipped: number;
  remapped: Record<string, string>;
  interrupted: number;
}

export class BackupFenceError extends Error {
  constructor() {
    super("durable task mutations are fenced for backup");
    this.name = "BackupFenceError";
  }
}

export class IdempotencyConflictError extends Error {
  constructor() {
    super("idempotency key was already used with different test arguments");
    this.name = "IdempotencyConflictError";
  }
}

export function digest(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export function canonicalRequestHash(arguments_: TestTaskArguments): string {
  return digest(
    JSON.stringify({
      target: arguments_.target,
      keyword: arguments_.keyword ?? null,
      markers: arguments_.markers ?? null,
      timeoutSeconds: arguments_.timeoutSeconds,
    }),
  );
}

export function makeTask(input: CreateTaskInput): TaskRecord {
  const idempotencyKeyHash = digest(input.idempotencyKey);
  return {
    id: randomUUID(),
    kind: "test-run",
    idempotencyKeyHash,
    requestHash: canonicalRequestHash(input.arguments),
    arguments: input.arguments,
    status: "queued",
    ownerInstance: null,
    leaseUntil: null,
    attempts: 0,
    stdout: "",
    stderr: "",
    exitCode: null,
    error: null,
    createdAt: input.now,
    updatedAt: input.now,
  };
}

export function portableTask(task: TaskRecord): TaskRecord {
  return {
    ...structuredClone(task),
    status: task.status === "running" || task.status === "queued" ? "interrupted" : task.status,
    ownerInstance: null,
    leaseUntil: null,
  };
}

export function taskDigest(task: TaskRecord): string {
  return digest(JSON.stringify(portableTask(task)));
}

export function deterministicTaskId(restoreId: string, taskId: string, value: string): string {
  const hex = digest(`${restoreId}\0${taskId}\0${value}`);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

export function parseBackupSnapshot(snapshot: string): {
  tasks: TaskRecord[];
  logs: Map<string, { stdout: string; stderr: string }>;
} {
  const tasks: TaskRecord[] = [];
  const logs = new Map<string, { stdout: string; stderr: string }>();
  let complete = false;
  for (const line of snapshot.split(/\r?\n/u).filter((value) => value.length > 0)) {
    const entry = JSON.parse(line) as {
      type?: string;
      schemaVersion?: number;
      task?: TaskRecord;
      record?: { taskId?: string; stdout?: string; stderr?: string };
    };
    if (entry.schemaVersion !== 1) throw new Error("unsupported stateless MCP backup schema");
    if (entry.type === "task" && entry.task !== undefined) tasks.push(entry.task);
    if (entry.type === "log" && entry.record?.taskId !== undefined) {
      logs.set(entry.record.taskId, { stdout: entry.record.stdout ?? "", stderr: entry.record.stderr ?? "" });
    }
    if (entry.type === "complete") complete = true;
  }
  if (!complete) throw new Error("stateless MCP backup is incomplete");
  return { tasks, logs };
}
