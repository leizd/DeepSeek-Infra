import {
  BackupFenceError,
  BACKUP_FENCE_TTL_MS,
  canonicalRequestHash,
  deterministicTaskId,
  digest,
  IdempotencyConflictError,
  makeTask,
  parseBackupSnapshot,
  portableTask,
  taskDigest,
  type CreateTaskInput,
  type CreateTaskResult,
  type TaskOutcome,
  type TaskRecord,
  type TaskStore,
  type BackupCapabilities,
  type BackupFence,
  type RestoreImportResult,
} from "./task-store.js";

export class MemoryTaskStore implements TaskStore {
  readonly tasks = new Map<string, TaskRecord>();
  readonly idempotency = new Map<string, string>();
  generation = 0;
  restoreEpoch = "initial";
  backupFence: BackupFence | null = null;

  private expireBackupFence(now: number): void {
    if (this.backupFence !== null && this.backupFence.expiresAt <= now) this.backupFence = null;
  }

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
    this.expireBackupFence(input.now);
    if (this.backupFence !== null) throw new BackupFenceError();
    this.tasks.set(candidate.id, candidate);
    this.idempotency.set(candidate.idempotencyKeyHash, candidate.id);
    this.generation += 1;
    return { task: structuredClone(candidate), deduplicated: false };
  }

  async get(taskId: string): Promise<TaskRecord | null> {
    const task = this.tasks.get(taskId);
    return task === undefined ? null : structuredClone(task);
  }

  async claim(instanceId: string, now: number, leaseMs: number): Promise<TaskRecord | null> {
    this.expireBackupFence(now);
    if (this.backupFence !== null) return null;
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
    this.generation += 1;
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
    this.generation += 1;
    return structuredClone(task);
  }

  async backupCapabilities(): Promise<BackupCapabilities> {
    return { contributorId: "stateless-mcp", schemaVersion: 1, dataClass: "durable", available: true };
  }

  async prepareBackup(backupId: string, now: number): Promise<BackupFence> {
    this.expireBackupFence(now);
    if (this.backupFence !== null && this.backupFence.backupId !== backupId) throw new BackupFenceError();
    this.backupFence ??= { backupId, generation: this.generation, createdAt: now, expiresAt: now + BACKUP_FENCE_TTL_MS };
    return structuredClone(this.backupFence);
  }

  async exportBackup(backupId: string): Promise<string> {
    if (this.backupFence?.backupId !== backupId) throw new Error("backup fence is not owned by this request");
    const generation = this.generation;
    const lines: unknown[] = [
      { type: "metadata", schemaVersion: 1, stateGeneration: generation, restoreEpoch: this.restoreEpoch },
    ];
    for (const task of [...this.tasks.values()].sort((left, right) => left.id.localeCompare(right.id))) {
      const portable = portableTask(task);
      lines.push({ type: "task", schemaVersion: 1, task: { ...portable, stdout: "", stderr: "" } });
      lines.push({ type: "idempotency", schemaVersion: 1, record: { hash: task.idempotencyKeyHash, taskId: task.id, requestHash: task.requestHash } });
      lines.push({ type: "log", schemaVersion: 1, record: { taskId: task.id, stdout: task.stdout, stderr: task.stderr } });
    }
    lines.push({ type: "complete", schemaVersion: 1, stateGeneration: this.generation });
    if (generation !== this.generation) throw new Error("state generation changed during backup");
    return `${lines.map((line) => JSON.stringify(line)).join("\n")}\n`;
  }

  async releaseBackup(backupId: string): Promise<void> {
    if (this.backupFence?.backupId === backupId) this.backupFence = null;
  }

  async restoreBackup(restoreId: string, snapshot: string, now: number): Promise<RestoreImportResult> {
    const parsed = parseBackupSnapshot(snapshot);
    const remapped: Record<string, string> = {};
    let imported = 0;
    let skipped = 0;
    let interrupted = 0;
    this.restoreEpoch = deterministicTaskId(restoreId, "restore-epoch", "v1");
    for (const source of parsed.tasks) {
      const log = parsed.logs.get(source.id) ?? { stdout: "", stderr: "" };
      const portable = portableTask({ ...source, ...log });
      if (portable.status === "interrupted") interrupted += 1;
      const existing = this.tasks.get(portable.id);
      if (existing !== undefined && taskDigest(existing) === taskDigest(portable)) {
        skipped += 1;
        continue;
      }
      let taskId = portable.id;
      if (existing !== undefined) {
        taskId = deterministicTaskId(restoreId, portable.id, taskDigest(portable));
        remapped[portable.id] = taskId;
        const remappedExisting = this.tasks.get(taskId);
        if (remappedExisting !== undefined) {
          const normalized = {
            ...remappedExisting,
            id: portable.id,
            idempotencyKeyHash: portable.idempotencyKeyHash,
          };
          if (taskDigest(normalized) === taskDigest(portable)) {
            skipped += 1;
            continue;
          }
          throw new Error("deterministic restore task collision");
        }
      }
      const restored = { ...portable, id: taskId };
      this.tasks.set(taskId, restored);
      let indexHash = restored.idempotencyKeyHash;
      const indexed = this.idempotency.get(indexHash);
      if (indexed !== undefined && indexed !== taskId) indexHash = digest(`${indexHash}\0${taskId}`);
      restored.idempotencyKeyHash = indexHash;
      this.idempotency.set(indexHash, taskId);
      imported += 1;
    }
    this.generation += imported > 0 ? 1 : 0;
    return { restoreId, restoreEpoch: this.restoreEpoch, imported, skipped, remapped, interrupted };
  }

  async close(): Promise<void> {}
}
