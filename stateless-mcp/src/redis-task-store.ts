import { createClient, type RedisClientType } from "redis";

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
  type BackupCapabilities,
  type BackupFence,
  type CreateTaskInput,
  type CreateTaskResult,
  type TaskOutcome,
  type TaskRecord,
  type TaskStore,
  type RestoreImportResult,
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
    return raw === null ? null : parseTask(raw);
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

  async close(): Promise<void> {
    await this.client.quit();
  }
}
