import {
  planRestoreDecisions,
  preparedDigestOf,
  type StagedRestoreDecision,
} from "./restore-plan.js";
import {
  BackupFenceError,
  BACKUP_FENCE_TTL_MS,
  canonicalRequestHash,
  deterministicTaskId,
  IdempotencyConflictError,
  makeTask,
  portableTask,
  RestoreConflictError,
  RestoreFenceError,
  RestoreStateError,
  taskDigest,
  type CreateTaskInput,
  type CreateTaskResult,
  type ExternalRestorePhase,
  type TaskOutcome,
  type TaskRecord,
  type TaskStore,
  type BackupCapabilities,
  type BackupFence,
  type RestoreJournal,
} from "./task-store.js";

export class MemoryTaskStore implements TaskStore {
  readonly tasks = new Map<string, TaskRecord>();
  readonly idempotency = new Map<string, string>();
  readonly restoreJournals = new Map<string, RestoreJournal>();
  generation = 0;
  restoreEpoch = "initial";
  backupFence: BackupFence | null = null;
  restoreFence: { restoreId: string; expiresAt: number } | null = null;

  private expireRestoreFence(now: number): void {
    if (this.restoreFence !== null && this.restoreFence.expiresAt <= now) this.restoreFence = null;
  }

  private requireJournal(restoreId: string): RestoreJournal {
    const journal = this.restoreJournals.get(restoreId);
    if (journal === undefined) throw new RestoreStateError(`restore ${restoreId} is unknown`);
    return journal;
  }

  private expectPhase(restoreId: string, expected: ExternalRestorePhase[], transactionDigest = ""): RestoreJournal {
    const journal = this.requireJournal(restoreId);
    if (!expected.includes(journal.phase)) {
      throw new RestoreStateError(`restore ${restoreId} is in phase ${journal.phase}`);
    }
    if (transactionDigest !== "" && journal.transactionDigest !== transactionDigest) {
      throw new RestoreStateError("restore transaction digest mismatch");
    }
    return journal;
  }

  private readonly staged = new Map<string, StagedRestoreDecision[]>();
  private readonly insertedKeys = new Map<string, { tasks: Set<string>; indexes: Set<string> }>();

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
    this.expireRestoreFence(input.now);
    if (this.backupFence !== null) throw new BackupFenceError();
    if (this.restoreFence !== null) throw new RestoreFenceError();
    this.tasks.set(candidate.id, candidate);
    this.idempotency.set(candidate.idempotencyKeyHash, candidate.id);
    this.generation += 1;
    return { task: structuredClone(candidate), deduplicated: false };
  }

  async get(taskId: string): Promise<TaskRecord | null> {
    const task = this.tasks.get(taskId);
    if (task === undefined || task.restorePending !== undefined) return null;
    return structuredClone(task);
  }

  async claim(instanceId: string, now: number, leaseMs: number): Promise<TaskRecord | null> {
    this.expireBackupFence(now);
    this.expireRestoreFence(now);
    if (this.backupFence !== null) return null;
    if (this.restoreFence !== null) return null;
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
    this.expireRestoreFence(now);
    if (this.restoreFence !== null) throw new RestoreFenceError();
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
    this.expireRestoreFence(now);
    if (this.restoreFence !== null) throw new RestoreFenceError();
    if (this.backupFence !== null && this.backupFence.backupId !== backupId) throw new BackupFenceError();
    this.backupFence ??= { backupId, generation: this.generation, createdAt: now, expiresAt: now + BACKUP_FENCE_TTL_MS };
    return structuredClone(this.backupFence);
  }

  async *exportBackup(backupId: string): AsyncGenerator<string, void, void> {
    if (this.backupFence?.backupId !== backupId) throw new Error("backup fence is not owned by this request");
    const generation = this.generation;
    yield `${JSON.stringify({ type: "metadata", schemaVersion: 1, stateGeneration: generation, restoreEpoch: this.restoreEpoch })}\n`;
    for (const task of [...this.tasks.values()].sort((left, right) => left.id.localeCompare(right.id))) {
      const portable = portableTask(task);
      yield `${JSON.stringify({ type: "task", schemaVersion: 1, task: { ...portable, stdout: "", stderr: "" } })}\n`;
      yield `${JSON.stringify({ type: "idempotency", schemaVersion: 1, record: { hash: task.idempotencyKeyHash, taskId: task.id, requestHash: task.requestHash } })}\n`;
      yield `${JSON.stringify({ type: "log", schemaVersion: 1, record: { taskId: task.id, stdout: task.stdout, stderr: task.stderr } })}\n`;
    }
    if (generation !== this.generation) throw new Error("state generation changed during backup");
    yield `${JSON.stringify({ type: "complete", schemaVersion: 1, stateGeneration: this.generation })}\n`;
  }

  async releaseBackup(backupId: string): Promise<void> {
    if (this.backupFence?.backupId === backupId) this.backupFence = null;
  }

  async restoreStatus(restoreId: string): Promise<RestoreJournal | null> {
    const journal = this.restoreJournals.get(restoreId);
    return journal === undefined ? null : structuredClone(journal);
  }

  async prepareRestore(
    restoreId: string,
    transactionDigest: string,
    source: AsyncIterable<string>,
    now: number,
  ): Promise<RestoreJournal> {
    const existing = this.restoreJournals.get(restoreId);
    if (existing !== undefined) {
      if (existing.transactionDigest !== transactionDigest) {
        throw new RestoreStateError("restore transaction digest mismatch");
      }
      if (existing.phase === "rolled-back") {
        throw new RestoreStateError(`restore ${restoreId} was rolled back`);
      }
      if (existing.phase !== "preparing" && existing.phase !== "aborting") {
        return structuredClone(existing);
      }
      this.staged.delete(restoreId);
    }
    const plan = await planRestoreDecisions(restoreId, source, async (taskIds, indexHashes) => {
      return {
        tasks: taskIds.map((id) => this.tasks.get(id) ?? null),
        indexed: indexHashes.map((hash) => this.idempotency.get(hash) ?? null),
      };
    });
    this.staged.set(restoreId, plan.decisions);
    const staged = this.staged.get(restoreId) ?? [];
    if (staged.length !== plan.decisions.length || preparedDigestOf(staged) !== plan.preparedDigest) {
      this.staged.delete(restoreId);
      throw new RestoreStateError("staged restore namespace failed read-back verification");
    }
    const journal: RestoreJournal = {
      contributorId: "stateless-mcp",
      schemaVersion: 1,
      restoreId,
      transactionDigest,
      sourceDigest: plan.sourceDigest,
      preparedDigest: plan.preparedDigest,
      phase: "prepared",
      records: plan.records,
      imported: plan.imported,
      skipped: plan.skipped,
      interrupted: plan.interrupted,
      remapped: plan.remapped,
      previousEpoch: this.restoreEpoch,
      restoreEpoch: deterministicTaskId(restoreId, "restore-epoch", "v1"),
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
    };
    this.restoreJournals.set(restoreId, journal);
    return structuredClone(journal);
  }

  async commitRestoreIntent(restoreId: string, transactionDigest: string, now: number): Promise<RestoreJournal> {
    const journal = this.expectPhase(restoreId, ["prepared", "commit-intent"], transactionDigest);
    journal.phase = "commit-intent";
    journal.updatedAt = now;
    return structuredClone(journal);
  }

  async commitRestore(restoreId: string, transactionDigest: string, now: number): Promise<RestoreJournal> {
    this.expireRestoreFence(now);
    const journal = this.restoreJournals.get(restoreId);
    if (journal === undefined) throw new RestoreStateError(`restore ${restoreId} is unknown`);
    if (journal.phase === "committed-pending-complete" && journal.transactionDigest === transactionDigest) {
      return structuredClone(journal);
    }
    if (journal.phase !== "committing") {
      this.expectPhase(restoreId, ["commit-intent"], transactionDigest);
      journal.phase = "committing";
      journal.updatedAt = now;
    } else if (journal.transactionDigest !== transactionDigest) {
      throw new RestoreStateError("restore transaction digest mismatch");
    }
    if (this.backupFence !== null) {
      throw new RestoreFenceError();
    }
    if (this.restoreFence !== null && this.restoreFence.restoreId !== restoreId) {
      throw new RestoreStateError("another restore holds the restore fence");
    }
    this.restoreFence = { restoreId, expiresAt: now + 3_600_000 };
    const decisions = this.staged.get(restoreId) ?? [];
    if (preparedDigestOf(decisions) !== journal.preparedDigest) {
      throw new RestoreStateError("staged restore namespace does not match the restore journal");
    }
    const inserted = this.insertedKeys.get(restoreId) ?? { tasks: new Set<string>(), indexes: new Set<string>() };
    this.insertedKeys.set(restoreId, inserted);
    for (const decision of decisions) {
      if (decision.action === "skip") {
        const live = this.tasks.get(decision.skipTargetId);
        if (live === undefined || taskDigest(live) !== decision.skipDigest) {
          throw new RestoreConflictError();
        }
        continue;
      }
      const existingRaw = this.tasks.get(decision.finalTaskId);
      if (existingRaw !== undefined && JSON.stringify(existingRaw) !== decision.taskJson) {
        throw new RestoreConflictError();
      }
      const indexed = this.idempotency.get(decision.idempotencyHash);
      if (indexed !== undefined && indexed !== decision.finalTaskId) {
        throw new RestoreConflictError();
      }
      this.tasks.set(decision.finalTaskId, JSON.parse(decision.taskJson) as TaskRecord);
      this.idempotency.set(decision.idempotencyHash, decision.finalTaskId);
      inserted.tasks.add(decision.finalTaskId);
      inserted.indexes.add(decision.idempotencyHash);
    }
    this.restoreEpoch = journal.restoreEpoch;
    if (journal.imported > 0) {
      this.generation += 1;
    }
    journal.phase = "committed-pending-complete";
    journal.updatedAt = now;
    return structuredClone(journal);
  }

  async completeRestore(restoreId: string, now: number): Promise<RestoreJournal> {
    const journal = this.expectPhase(restoreId, ["committed-pending-complete", "complete"]);
    if (journal.phase === "complete") {
      return structuredClone(journal);
    }
    const inserted = this.insertedKeys.get(restoreId);
    if (inserted !== undefined) {
      for (const taskId of inserted.tasks) {
        const task = this.tasks.get(taskId);
        if (task !== undefined && task.restorePending === restoreId) {
          delete task.restorePending;
        }
      }
    }
    this.staged.delete(restoreId);
    this.insertedKeys.delete(restoreId);
    if (this.restoreFence?.restoreId === restoreId) {
      this.restoreFence = null;
    }
    journal.phase = "complete";
    journal.updatedAt = now;
    return structuredClone(journal);
  }

  async abortRestore(restoreId: string, now: number): Promise<RestoreJournal> {
    const journal = this.restoreJournals.get(restoreId);
    if (journal === undefined) {
      return {
        contributorId: "stateless-mcp",
        schemaVersion: 1,
        restoreId,
        transactionDigest: "",
        sourceDigest: "",
        preparedDigest: "",
        phase: "rolled-back",
        records: 0,
        imported: 0,
        skipped: 0,
        interrupted: 0,
        remapped: {},
        previousEpoch: "initial",
        restoreEpoch: "",
        createdAt: now,
        updatedAt: now,
      };
    }
    if (journal.phase === "rolled-back") return structuredClone(journal);
    if (journal.phase === "complete") {
      throw new RestoreStateError(`restore ${restoreId} is already complete`);
    }
    journal.phase = "aborting";
    journal.updatedAt = now;
    const decisions = this.staged.get(restoreId) ?? [];
    for (const decision of decisions) {
      if (decision.action !== "insert") continue;
      const existing = this.tasks.get(decision.finalTaskId);
      if (existing !== undefined && JSON.stringify(existing) === decision.taskJson) {
        this.tasks.delete(decision.finalTaskId);
      }
      if (this.idempotency.get(decision.idempotencyHash) === decision.finalTaskId) {
        this.idempotency.delete(decision.idempotencyHash);
      }
    }
    if (this.restoreEpoch === journal.restoreEpoch) {
      this.restoreEpoch = journal.previousEpoch;
    }
    this.staged.delete(restoreId);
    this.insertedKeys.delete(restoreId);
    if (this.restoreFence?.restoreId === restoreId) {
      this.restoreFence = null;
    }
    journal.phase = "rolled-back";
    journal.updatedAt = now;
    return structuredClone(journal);
  }

  async close(): Promise<void> {}
}
