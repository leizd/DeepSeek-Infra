import { createHash, randomUUID } from "node:crypto";

export type TaskStatus = "queued" | "running" | "succeeded" | "failed";

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
  close(): Promise<void>;
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
