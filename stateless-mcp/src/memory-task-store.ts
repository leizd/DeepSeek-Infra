import {
  canonicalRequestHash,
  IdempotencyConflictError,
  makeTask,
  type CreateTaskInput,
  type CreateTaskResult,
  type TaskOutcome,
  type TaskRecord,
  type TaskStore,
} from "./task-store.js";

export class MemoryTaskStore implements TaskStore {
  readonly tasks = new Map<string, TaskRecord>();
  readonly idempotency = new Map<string, string>();

  async createOrGet(input: CreateTaskInput): Promise<CreateTaskResult> {
    const candidate = makeTask(input);
    const existingId = this.idempotency.get(candidate.idempotencyKeyHash);
    if (existingId !== undefined) {
      const existing = this.tasks.get(existingId);
      if (existing === undefined) {
        throw new Error("idempotency index points to a missing task");
      }
      if (existing.requestHash !== canonicalRequestHash(input.arguments)) {
        throw new IdempotencyConflictError();
      }
      return { task: structuredClone(existing), deduplicated: true };
    }
    this.tasks.set(candidate.id, candidate);
    this.idempotency.set(candidate.idempotencyKeyHash, candidate.id);
    return { task: structuredClone(candidate), deduplicated: false };
  }

  async get(taskId: string): Promise<TaskRecord | null> {
    const task = this.tasks.get(taskId);
    return task === undefined ? null : structuredClone(task);
  }

  async claim(instanceId: string, now: number, leaseMs: number): Promise<TaskRecord | null> {
    const eligible = [...this.tasks.values()]
      .filter(
        (task) =>
          task.status === "queued" ||
          (task.status === "running" && task.leaseUntil !== null && task.leaseUntil <= now),
      )
      .sort((left, right) => left.createdAt - right.createdAt)[0];
    if (eligible === undefined) {
      return null;
    }
    eligible.status = "running";
    eligible.ownerInstance = instanceId;
    eligible.leaseUntil = now + leaseMs;
    eligible.attempts += 1;
    eligible.updatedAt = now;
    eligible.error = null;
    return structuredClone(eligible);
  }

  async heartbeat(taskId: string, instanceId: string, now: number, leaseMs: number): Promise<boolean> {
    const task = this.tasks.get(taskId);
    if (task === undefined || task.status !== "running" || task.ownerInstance !== instanceId) {
      return false;
    }
    task.leaseUntil = now + leaseMs;
    task.updatedAt = now;
    return true;
  }

  async complete(
    taskId: string,
    instanceId: string,
    outcome: TaskOutcome,
    now: number,
  ): Promise<TaskRecord | null> {
    const task = this.tasks.get(taskId);
    if (task === undefined || task.status !== "running" || task.ownerInstance !== instanceId) {
      return null;
    }
    task.status = outcome.error === null && outcome.exitCode === 0 ? "succeeded" : "failed";
    task.stdout = outcome.stdout;
    task.stderr = outcome.stderr;
    task.exitCode = outcome.exitCode;
    task.error = outcome.error;
    task.leaseUntil = null;
    task.updatedAt = now;
    return structuredClone(task);
  }

  async close(): Promise<void> {}
}
