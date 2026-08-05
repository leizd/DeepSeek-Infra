import { createClient, type RedisClientType } from "redis";

import { planRestoreDecisions, preparedDigestOf, type StagedRestoreDecision } from "./restore-plan.js";
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
  RestoreConflictError,
  RestoreStateError,
  RESTORE_FENCE_TTL_MS,
  RESTORE_JOURNAL_TTL_MS,
  taskDigest,
  type BackupCapabilities,
  type BackupFence,
  type CreateTaskInput,
  type CreateTaskResult,
  type ExternalRestorePhase,
  type TaskOutcome,
  type TaskRecord,
  type TaskStore,
  type RestoreImportResult,
  type RestoreJournal,
} from "./task-store.js";

const CREATE_SCRIPT = `
local existing_id = redis.call("GET", KEYS[1])
if existing_id then
  local existing = redis.call("GET", ARGV[3] .. existing_id)
  return {"existing", existing or ""}
end
if redis.call("EXISTS", KEYS[4]) == 1 then return redis.error_reply("BACKUP_FENCED") end
redis.call("SET", KEYS[1], ARGV[1])
redis.call("SET", KEYS[2], ARGV[2])
redis.call("ZADD", KEYS[3], ARGV[4], ARGV[1])
redis.call("INCR", KEYS[5])
return {"created", ARGV[2]}
`;

const CLAIM_SCRIPT = `
if redis.call("EXISTS", KEYS[2]) == 1 then return "" end
local ids = redis.call("ZRANGEBYSCORE", KEYS[1], "-inf", ARGV[1], "LIMIT", 0, 16)
for _, id in ipairs(ids) do
  local key = ARGV[4] .. id
  local raw = redis.call("GET", key)
  if not raw then
    redis.call("ZREM", KEYS[1], id)
  else
    local task = cjson.decode(raw)
    local claimable = task.status == "queued"
    if task.status == "running" and task.leaseUntil ~= cjson.null and task.leaseUntil <= tonumber(ARGV[1]) then
      claimable = true
    end
    if claimable then
      task.status = "running"
      task.ownerInstance = ARGV[2]
      task.leaseUntil = tonumber(ARGV[1]) + tonumber(ARGV[3])
      task.attempts = task.attempts + 1
      task.updatedAt = tonumber(ARGV[1])
      task.error = cjson.null
      local encoded = cjson.encode(task)
      redis.call("SET", key, encoded)
      redis.call("ZADD", KEYS[1], task.leaseUntil, id)
      redis.call("INCR", KEYS[3])
      return encoded
    end
  end
end
return ""
`;

const HEARTBEAT_SCRIPT = `
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local task = cjson.decode(raw)
if task.status ~= "running" or task.ownerInstance ~= ARGV[1] then return 0 end
task.leaseUntil = tonumber(ARGV[2]) + tonumber(ARGV[3])
task.updatedAt = tonumber(ARGV[2])
redis.call("SET", KEYS[1], cjson.encode(task))
redis.call("ZADD", KEYS[2], task.leaseUntil, task.id)
return 1
`;

const COMPLETE_SCRIPT = `
local raw = redis.call("GET", KEYS[1])
if not raw then return "" end
local task = cjson.decode(raw)
if task.status ~= "running" or task.ownerInstance ~= ARGV[1] then return "" end
local outcome = cjson.decode(ARGV[2])
if outcome.error == cjson.null and outcome.exitCode == 0 then
  task.status = "succeeded"
else
  task.status = "failed"
end
task.stdout = outcome.stdout
task.stderr = outcome.stderr
task.exitCode = outcome.exitCode
task.error = outcome.error
task.leaseUntil = cjson.null
task.updatedAt = tonumber(ARGV[3])
local encoded = cjson.encode(task)
redis.call("SET", KEYS[1], encoded)
redis.call("ZREM", KEYS[2], task.id)
redis.call("INCR", KEYS[3])
return encoded
`;

const RESTORE_JOURNAL_TRANSITION_SCRIPT = `
local raw = redis.call("GET", KEYS[1])
if not raw then return "missing" end
local journal = cjson.decode(raw)
local expected = cjson.decode(ARGV[1])
local matched = false
for _, phase in ipairs(expected) do
  if journal.phase == phase then matched = true end
end
if not matched then return "phase:" .. tostring(journal.phase) end
if ARGV[3] ~= "" and journal.transactionDigest ~= ARGV[3] then return "digest" end
journal.phase = ARGV[2]
journal.updatedAt = tonumber(ARGV[4])
redis.call("SET", KEYS[1], cjson.encode(journal), "PX", ARGV[5])
return cjson.encode(journal)
`;

const RESTORE_FENCE_ACQUIRE_SCRIPT = `
if redis.call("GET", KEYS[2]) then return "backup-fenced" end
local raw = redis.call("GET", KEYS[1])
if raw then
  local fence = cjson.decode(raw)
  if fence.restoreId ~= ARGV[1] then return "fenced" end
  redis.call("PEXPIRE", KEYS[1], ARGV[3])
  return "ok"
end
redis.call("SET", KEYS[1], cjson.encode({restoreId = ARGV[1], createdAt = tonumber(ARGV[2]), expiresAt = tonumber(ARGV[2]) + tonumber(ARGV[3])}), "PX", ARGV[3])
return "ok"
`;

const RESTORE_FENCE_RELEASE_SCRIPT = `
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local fence = cjson.decode(raw)
if fence.restoreId ~= ARGV[1] then return 0 end
redis.call("DEL", KEYS[1])
return 1
`;

const RESTORE_INSTALL_SCRIPT = `
local existing = redis.call("GET", KEYS[1])
if existing then
  if existing ~= ARGV[1] then return "conflict" end
else
  redis.call("SET", KEYS[1], ARGV[1])
end
local indexed = redis.call("GET", KEYS[2])
if indexed then
  if indexed ~= ARGV[2] then return "conflict" end
else
  redis.call("SET", KEYS[2], ARGV[2])
end
redis.call("SADD", KEYS[3], KEYS[1])
redis.call("SADD", KEYS[4], KEYS[2])
return "ok"
`;

const RESTORE_UNINSTALL_SCRIPT = `
local existing = redis.call("GET", KEYS[1])
if existing and existing == ARGV[1] then
  redis.call("DEL", KEYS[1])
end
local indexed = redis.call("GET", KEYS[2])
if indexed and indexed == ARGV[2] then
  redis.call("DEL", KEYS[2])
end
return 1
`;

const RESTORE_CLEAR_PENDING_SCRIPT = `
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local task = cjson.decode(raw)
if task.restorePending == cjson.null or task.restorePending == nil then return 0 end
if task.restorePending ~= ARGV[1] then return 0 end
task.restorePending = nil
redis.call("SET", KEYS[1], cjson.encode(task))
return 1
`;

const RESTORE_FINALIZE_COMMIT_SCRIPT = `
local raw = redis.call("GET", KEYS[1])
if not raw then return "missing" end
local journal = cjson.decode(raw)
if journal.transactionDigest ~= ARGV[1] then return "digest" end
if journal.phase == "committed-pending-complete" then return cjson.encode(journal) end
if journal.phase ~= "committing" then return "phase:" .. tostring(journal.phase) end
redis.call("SET", KEYS[2], journal.restoreEpoch)
if journal.imported > 0 then redis.call("INCR", KEYS[3]) end
journal.phase = "committed-pending-complete"
journal.updatedAt = tonumber(ARGV[2])
redis.call("SET", KEYS[1], cjson.encode(journal), "PX", ARGV[3])
return cjson.encode(journal)
`;

const RESTORE_RESET_EPOCH_SCRIPT = `
local raw = redis.call("GET", KEYS[1])
if raw and raw == ARGV[1] then
  redis.call("SET", KEYS[1], ARGV[2])
end
return 1
`;

function parseTask(raw: string): TaskRecord {
  return JSON.parse(raw) as TaskRecord;
}

export class RedisTaskStore implements TaskStore {
  private constructor(
    private readonly client: RedisClientType,
    private readonly prefix: string,
  ) {}

  static async connect(url: string, prefix: string): Promise<RedisTaskStore> {
    const client = createClient({ url });
    client.on("error", (error) => {
      console.error("redis_error", error);
    });
    await client.connect();
    return new RedisTaskStore(client as RedisClientType, prefix);
  }

  private taskKey(taskId: string): string {
    return `${this.prefix}:task:${taskId}`;
  }

  private idempotencyKey(hash: string): string {
    return `${this.prefix}:idempotency:${hash}`;
  }

  private queueKey(): string {
    return `${this.prefix}:queue`;
  }

  private generationKey(): string {
    return `${this.prefix}:state-generation`;
  }

  private backupFenceKey(): string {
    return `${this.prefix}:backup-fence`;
  }

  private restoreEpochKey(): string {
    return `${this.prefix}:restore-epoch`;
  }

  private restoreFenceKey(): string {
    return `${this.prefix}:restore-fence`;
  }

  private restoreJournalKey(restoreId: string): string {
    return `${this.prefix}:restore:${restoreId}:journal`;
  }

  private restoreStagedTaskKey(restoreId: string, taskId: string): string {
    return `${this.prefix}:restore:${restoreId}:staged-task:${taskId}`;
  }

  private restoreStagedIdempotencyKey(restoreId: string, hash: string): string {
    return `${this.prefix}:restore:${restoreId}:staged-idempotency:${hash}`;
  }

  private restoreInsertedTasksKey(restoreId: string): string {
    return `${this.prefix}:restore:${restoreId}:inserted-tasks`;
  }

  private restoreInsertedIndexesKey(restoreId: string): string {
    return `${this.prefix}:restore:${restoreId}:inserted-indexes`;
  }

  async createOrGet(input: CreateTaskInput): Promise<CreateTaskResult> {
    const task = makeTask(input);
    let response: [string, string];
    try {
      response = (await this.client.eval(CREATE_SCRIPT, {
        keys: [
          this.idempotencyKey(task.idempotencyKeyHash),
          this.taskKey(task.id),
          this.queueKey(),
          this.backupFenceKey(),
          this.generationKey(),
        ],
        arguments: [task.id, JSON.stringify(task), `${this.prefix}:task:`, String(input.now)],
      })) as [string, string];
    } catch (error) {
      if (error instanceof Error && error.message.includes("BACKUP_FENCED")) throw new BackupFenceError();
      throw error;
    }
    const result = parseTask(response[1]);
    if (result.requestHash !== canonicalRequestHash(input.arguments)) {
      throw new IdempotencyConflictError();
    }
    return { task: result, deduplicated: response[0] === "existing" };
  }

  async get(taskId: string): Promise<TaskRecord | null> {
    const raw = await this.client.get(this.taskKey(taskId));
    if (raw === null) return null;
    const task = parseTask(raw);
    return task.restorePending === undefined ? task : null;
  }

  async claim(instanceId: string, now: number, leaseMs: number): Promise<TaskRecord | null> {
    const raw = (await this.client.eval(CLAIM_SCRIPT, {
      keys: [this.queueKey(), this.backupFenceKey(), this.generationKey()],
      arguments: [String(now), instanceId, String(leaseMs), `${this.prefix}:task:`],
    })) as string;
    return raw === "" ? null : parseTask(raw);
  }

  async heartbeat(taskId: string, instanceId: string, now: number, leaseMs: number): Promise<boolean> {
    const result = (await this.client.eval(HEARTBEAT_SCRIPT, {
      keys: [this.taskKey(taskId), this.queueKey()],
      arguments: [instanceId, String(now), String(leaseMs)],
    })) as number;
    return result === 1;
  }

  async complete(
    taskId: string,
    instanceId: string,
    outcome: TaskOutcome,
    now: number,
  ): Promise<TaskRecord | null> {
    const raw = (await this.client.eval(COMPLETE_SCRIPT, {
      keys: [this.taskKey(taskId), this.queueKey(), this.generationKey()],
      arguments: [instanceId, JSON.stringify(outcome), String(now)],
    })) as string;
    return raw === "" ? null : parseTask(raw);
  }

  async backupCapabilities(): Promise<BackupCapabilities> {
    await this.client.ping();
    return { contributorId: "stateless-mcp", schemaVersion: 1, dataClass: "durable", available: true };
  }

  async prepareBackup(backupId: string, now: number): Promise<BackupFence> {
    const generation = Number.parseInt((await this.client.get(this.generationKey())) ?? "0", 10);
    const fence: BackupFence = { backupId, generation, createdAt: now, expiresAt: now + BACKUP_FENCE_TTL_MS };
    const created = await this.client.set(this.backupFenceKey(), JSON.stringify(fence), { NX: true, PX: BACKUP_FENCE_TTL_MS });
    if (created === null) {
      const existing = JSON.parse((await this.client.get(this.backupFenceKey())) ?? "{}") as Partial<BackupFence>;
      if (existing.backupId !== backupId) throw new BackupFenceError();
      return existing as BackupFence;
    }
    return fence;
  }

  async exportBackup(backupId: string): Promise<string> {
    const fence = JSON.parse((await this.client.get(this.backupFenceKey())) ?? "{}") as Partial<BackupFence>;
    if (fence.backupId !== backupId) throw new Error("backup fence is not owned by this request");
    const generation = Number.parseInt((await this.client.get(this.generationKey())) ?? "0", 10);
    const restoreEpoch = (await this.client.get(this.restoreEpochKey())) ?? "initial";
    const keys: string[] = [];
    let cursor = "0";
    do {
      const page = await this.client.scan(cursor, { MATCH: `${this.prefix}:task:*`, COUNT: 200 });
      cursor = page.cursor;
      keys.push(...page.keys);
    } while (cursor !== "0");
    const lines: unknown[] = [{ type: "metadata", schemaVersion: 1, stateGeneration: generation, restoreEpoch }];
    const tasks = (await Promise.all(keys.map(async (key) => await this.client.get(key))))
      .filter((raw): raw is string => raw !== null)
      .map(parseTask)
      .sort((left, right) => left.id.localeCompare(right.id));
    for (const task of tasks) {
      const portable = portableTask(task);
      lines.push({ type: "task", schemaVersion: 1, task: { ...portable, stdout: "", stderr: "" } });
      lines.push({ type: "idempotency", schemaVersion: 1, record: { hash: task.idempotencyKeyHash, taskId: task.id, requestHash: task.requestHash } });
      lines.push({ type: "log", schemaVersion: 1, record: { taskId: task.id, stdout: task.stdout, stderr: task.stderr } });
    }
    const endGeneration = Number.parseInt((await this.client.get(this.generationKey())) ?? "0", 10);
    lines.push({ type: "complete", schemaVersion: 1, stateGeneration: endGeneration });
    if (generation !== endGeneration) throw new Error("state generation changed during backup");
    return `${lines.map((line) => JSON.stringify(line)).join("\n")}\n`;
  }

  async releaseBackup(backupId: string): Promise<void> {
    const raw = await this.client.get(this.backupFenceKey());
    if (raw !== null && (JSON.parse(raw) as Partial<BackupFence>).backupId === backupId) {
      await this.client.del(this.backupFenceKey());
    }
  }

  async restoreBackup(restoreId: string, snapshot: string, now: number): Promise<RestoreImportResult> {
    const parsed = parseBackupSnapshot(snapshot);
    const restoreEpoch = deterministicTaskId(restoreId, "restore-epoch", "v1");
    const remapped: Record<string, string> = {};
    let imported = 0;
    let skipped = 0;
    let interrupted = 0;
    for (const source of parsed.tasks) {
      const log = parsed.logs.get(source.id) ?? { stdout: "", stderr: "" };
      const portable = portableTask({ ...source, ...log });
      if (portable.status === "interrupted") interrupted += 1;
      const existingRaw = await this.client.get(this.taskKey(portable.id));
      if (existingRaw !== null && taskDigest(parseTask(existingRaw)) === taskDigest(portable)) {
        skipped += 1;
        continue;
      }
      let taskId = portable.id;
      if (existingRaw !== null) {
        taskId = deterministicTaskId(restoreId, portable.id, taskDigest(portable));
        remapped[portable.id] = taskId;
        const remappedRaw = await this.client.get(this.taskKey(taskId));
        if (remappedRaw !== null) {
          const normalized = {
            ...parseTask(remappedRaw),
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
      let indexHash = restored.idempotencyKeyHash;
      const indexed = await this.client.get(this.idempotencyKey(indexHash));
      if (indexed !== null && indexed !== taskId) indexHash = digest(`${indexHash}\0${taskId}`);
      restored.idempotencyKeyHash = indexHash;
      const writes = await this.client.multi()
        .set(this.taskKey(taskId), JSON.stringify(restored), { NX: true })
        .set(this.idempotencyKey(indexHash), taskId, { NX: true })
        .exec();
      if (writes[0] === null) {
        const raced = await this.client.get(this.taskKey(taskId));
        if (raced === null) throw new Error("restore task write did not converge");
        const normalized = { ...parseTask(raced), id: portable.id, idempotencyKeyHash: portable.idempotencyKeyHash };
        if (taskDigest(normalized) !== taskDigest(portable)) throw new Error("restore task write collision");
        skipped += 1;
        continue;
      }
      imported += 1;
    }
    await this.client.multi()
      .set(this.restoreEpochKey(), restoreEpoch)
      .incrBy(this.generationKey(), imported > 0 ? 1 : 0)
      .exec();
    return { restoreId, restoreEpoch, imported, skipped, remapped, interrupted };
  }

  async restoreStatus(restoreId: string): Promise<RestoreJournal | null> {
    const raw = await this.client.get(this.restoreJournalKey(restoreId));
    return raw === null ? null : (JSON.parse(raw) as RestoreJournal);
  }

  private async saveRestoreJournal(journal: RestoreJournal): Promise<void> {
    journal.updatedAt = Date.now();
    await this.client.set(this.restoreJournalKey(journal.restoreId), JSON.stringify(journal), {
      PX: RESTORE_JOURNAL_TTL_MS,
    });
  }

  private async transitionRestoreJournal(
    restoreId: string,
    expected: ExternalRestorePhase[],
    next: ExternalRestorePhase,
    transactionDigest = "",
  ): Promise<RestoreJournal> {
    const result = (await this.client.eval(RESTORE_JOURNAL_TRANSITION_SCRIPT, {
      keys: [this.restoreJournalKey(restoreId)],
      arguments: [
        JSON.stringify(expected),
        next,
        transactionDigest,
        String(Date.now()),
        String(RESTORE_JOURNAL_TTL_MS),
      ],
    })) as string;
    if (result === "missing") throw new RestoreStateError(`restore ${restoreId} is unknown`);
    if (result === "digest") throw new RestoreStateError("restore transaction digest mismatch");
    if (result.startsWith("phase:")) {
      throw new RestoreStateError(`restore ${restoreId} is in phase ${result.slice(6)}`);
    }
    return JSON.parse(result) as RestoreJournal;
  }

  private async deleteStagedNamespace(restoreId: string): Promise<void> {
    for (const pattern of [
      `${this.prefix}:restore:${restoreId}:staged-task:*`,
      `${this.prefix}:restore:${restoreId}:staged-idempotency:*`,
    ]) {
      let cursor = "0";
      do {
        const page = await this.client.scan(cursor, { MATCH: pattern, COUNT: 500 });
        cursor = page.cursor;
        if (page.keys.length > 0) {
          await this.client.del(page.keys);
        }
      } while (cursor !== "0");
    }
    await this.client.del([this.restoreInsertedTasksKey(restoreId), this.restoreInsertedIndexesKey(restoreId)]);
  }

  private async stagedDecisions(restoreId: string): Promise<StagedRestoreDecision[]> {
    const decisions: StagedRestoreDecision[] = [];
    let cursor = "0";
    do {
      const page = await this.client.scan(cursor, {
        MATCH: `${this.prefix}:restore:${restoreId}:staged-task:*`,
        COUNT: 500,
      });
      cursor = page.cursor;
      if (page.keys.length > 0) {
        const values = await this.client.mGet(page.keys);
        for (const raw of values) {
          if (raw !== null) decisions.push(JSON.parse(raw) as StagedRestoreDecision);
        }
      }
    } while (cursor !== "0");
    return decisions;
  }

  async prepareRestore(
    restoreId: string,
    transactionDigest: string,
    source: AsyncIterable<string>,
    now: number,
  ): Promise<RestoreJournal> {
    const existing = await this.restoreStatus(restoreId);
    if (existing !== null) {
      if (existing.transactionDigest !== transactionDigest) {
        throw new RestoreStateError("restore transaction digest mismatch");
      }
      if (existing.phase === "rolled-back") {
        throw new RestoreStateError(`restore ${restoreId} was rolled back`);
      }
      if (existing.phase !== "preparing" && existing.phase !== "aborting") {
        return existing;
      }
      await this.deleteStagedNamespace(restoreId);
    }

    const plan = await planRestoreDecisions(
      restoreId,
      source,
      async (taskIds, indexHashes) => {
        const [tasks, indexed] = await Promise.all([
          taskIds.length > 0 ? this.client.mGet(taskIds.map((id) => this.taskKey(id))) : Promise.resolve([]),
          indexHashes.length > 0
            ? this.client.mGet(indexHashes.map((hash) => this.idempotencyKey(hash)))
            : Promise.resolve([]),
        ]);
        return {
          tasks: tasks.map((raw) => (raw === null ? null : parseTask(raw))),
          indexed,
        };
      },
    );

    try {
      const pipeline = this.client.multi();
      for (const decision of plan.decisions) {
        pipeline.set(this.restoreStagedTaskKey(restoreId, decision.finalTaskId), JSON.stringify(decision), {
          PX: RESTORE_JOURNAL_TTL_MS,
        });
        if (decision.action === "insert") {
          pipeline.set(this.restoreStagedIdempotencyKey(restoreId, decision.idempotencyHash), decision.finalTaskId, {
            PX: RESTORE_JOURNAL_TTL_MS,
          });
        }
      }
      await pipeline.exec();

      const staged = await this.stagedDecisions(restoreId);
      if (staged.length !== plan.decisions.length || preparedDigestOf(staged) !== plan.preparedDigest) {
        throw new RestoreStateError("staged restore namespace failed read-back verification");
      }
    } catch (error) {
      await this.deleteStagedNamespace(restoreId);
      throw error;
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
      previousEpoch: (await this.client.get(this.restoreEpochKey())) ?? "initial",
      restoreEpoch: deterministicTaskId(restoreId, "restore-epoch", "v1"),
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
    };
    await this.saveRestoreJournal(journal);
    return journal;
  }

  async commitRestoreIntent(restoreId: string, transactionDigest: string, now: number): Promise<RestoreJournal> {
    const journal = await this.restoreStatus(restoreId);
    if (journal !== null && journal.phase === "commit-intent" && journal.transactionDigest === transactionDigest) {
      return journal;
    }
    const transitioned = await this.transitionRestoreJournal(restoreId, ["prepared"], "commit-intent", transactionDigest);
    transitioned.updatedAt = now;
    return transitioned;
  }

  async commitRestore(restoreId: string, transactionDigest: string, now: number): Promise<RestoreJournal> {
    const current = await this.restoreStatus(restoreId);
    if (current === null) throw new RestoreStateError(`restore ${restoreId} is unknown`);
    if (current.phase === "committed-pending-complete" && current.transactionDigest === transactionDigest) {
      return current;
    }
    if (current.phase !== "committing") {
      await this.transitionRestoreJournal(restoreId, ["commit-intent"], "committing", transactionDigest);
    } else if (current.transactionDigest !== transactionDigest) {
      throw new RestoreStateError("restore transaction digest mismatch");
    }

    const fence = (await this.client.eval(RESTORE_FENCE_ACQUIRE_SCRIPT, {
      keys: [this.restoreFenceKey(), this.backupFenceKey()],
      arguments: [restoreId, String(now), String(RESTORE_FENCE_TTL_MS)],
    })) as string;
    if (fence === "backup-fenced") throw new RestoreStateError("a backup fence is active");
    if (fence !== "ok") throw new RestoreStateError("another restore holds the restore fence");

    const decisions = await this.stagedDecisions(restoreId);
    const journal = await this.restoreStatus(restoreId);
    if (journal === null || preparedDigestOf(decisions) !== journal.preparedDigest) {
      throw new RestoreStateError("staged restore namespace does not match the restore journal");
    }

    let installed = 0;
    for (const decision of decisions) {
      if (decision.action === "skip") {
        const raw = await this.client.get(this.taskKey(decision.skipTargetId));
        if (raw === null || taskDigest(parseTask(raw)) !== decision.skipDigest) {
          throw new RestoreConflictError();
        }
        continue;
      }
      const result = (await this.client.eval(RESTORE_INSTALL_SCRIPT, {
        keys: [
          this.taskKey(decision.finalTaskId),
          this.idempotencyKey(decision.idempotencyHash),
          this.restoreInsertedTasksKey(restoreId),
          this.restoreInsertedIndexesKey(restoreId),
        ],
        arguments: [decision.taskJson, decision.finalTaskId],
      })) as string;
      if (result !== "ok") throw new RestoreConflictError();
      installed += 1;
      if (installed % 500 === 0) {
        await this.client.pExpire(this.restoreFenceKey(), RESTORE_FENCE_TTL_MS);
      }
    }

    const finalized = (await this.client.eval(RESTORE_FINALIZE_COMMIT_SCRIPT, {
      keys: [this.restoreJournalKey(restoreId), this.restoreEpochKey(), this.generationKey()],
      arguments: [transactionDigest, String(Date.now()), String(RESTORE_JOURNAL_TTL_MS)],
    })) as string;
    if (finalized === "missing") throw new RestoreStateError(`restore ${restoreId} is unknown`);
    if (finalized === "digest") throw new RestoreStateError("restore transaction digest mismatch");
    if (finalized.startsWith("phase:")) {
      throw new RestoreStateError(`restore ${restoreId} is in phase ${finalized.slice(6)}`);
    }
    return JSON.parse(finalized) as RestoreJournal;
  }

  async completeRestore(restoreId: string, now: number): Promise<RestoreJournal> {
    const journal = await this.restoreStatus(restoreId);
    if (journal === null) throw new RestoreStateError(`restore ${restoreId} is unknown`);
    if (journal.phase === "complete") return journal;
    if (journal.phase !== "committed-pending-complete") {
      throw new RestoreStateError(`restore ${restoreId} is in phase ${journal.phase}`);
    }

    let cursor = "0";
    do {
      const page = await this.client.sScan(this.restoreInsertedTasksKey(restoreId), cursor, { COUNT: 500 });
      cursor = page.cursor;
      for (const key of page.members) {
        await this.client.eval(RESTORE_CLEAR_PENDING_SCRIPT, {
          keys: [key],
          arguments: [restoreId],
        });
      }
    } while (cursor !== "0");

    await this.deleteStagedNamespace(restoreId);
    await this.client.eval(RESTORE_FENCE_RELEASE_SCRIPT, {
      keys: [this.restoreFenceKey()],
      arguments: [restoreId],
    });
    journal.phase = "complete";
    journal.updatedAt = now;
    await this.saveRestoreJournal(journal);
    return journal;
  }

  async abortRestore(restoreId: string, now: number): Promise<RestoreJournal> {
    const journal = await this.restoreStatus(restoreId);
    if (journal === null) {
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
    if (journal.phase === "rolled-back") return journal;
    if (journal.phase === "complete") {
      throw new RestoreStateError(`restore ${restoreId} is already complete`);
    }
    if (journal.phase !== "aborting") {
      await this.transitionRestoreJournal(
        restoreId,
        ["preparing", "prepared", "commit-intent", "committing", "committed-pending-complete"],
        "aborting",
      );
    }

    const decisions = await this.stagedDecisions(restoreId);
    for (const decision of decisions) {
      if (decision.action !== "insert") continue;
      await this.client.eval(RESTORE_UNINSTALL_SCRIPT, {
        keys: [this.taskKey(decision.finalTaskId), this.idempotencyKey(decision.idempotencyHash)],
        arguments: [decision.taskJson, decision.finalTaskId],
      });
    }
    await this.client.eval(RESTORE_RESET_EPOCH_SCRIPT, {
      keys: [this.restoreEpochKey()],
      arguments: [journal.restoreEpoch, journal.previousEpoch],
    });
    await this.deleteStagedNamespace(restoreId);
    await this.client.eval(RESTORE_FENCE_RELEASE_SCRIPT, {
      keys: [this.restoreFenceKey()],
      arguments: [restoreId],
    });
    const aborted = await this.restoreStatus(restoreId);
    if (aborted === null) throw new RestoreStateError(`restore ${restoreId} is unknown`);
    aborted.phase = "rolled-back";
    aborted.updatedAt = now;
    await this.saveRestoreJournal(aborted);
    return aborted;
  }

  async close(): Promise<void> {
    await this.client.quit();
  }
}
