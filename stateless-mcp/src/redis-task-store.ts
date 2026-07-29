import { createClient, type RedisClientType } from "redis";

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

const CREATE_SCRIPT = `
local existing_id = redis.call("GET", KEYS[1])
if existing_id then
  local existing = redis.call("GET", ARGV[3] .. existing_id)
  return {"existing", existing or ""}
end
redis.call("SET", KEYS[1], ARGV[1])
redis.call("SET", KEYS[2], ARGV[2])
redis.call("ZADD", KEYS[3], ARGV[4], ARGV[1])
return {"created", ARGV[2]}
`;

const CLAIM_SCRIPT = `
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

  async createOrGet(input: CreateTaskInput): Promise<CreateTaskResult> {
    const task = makeTask(input);
    const response = (await this.client.eval(CREATE_SCRIPT, {
      keys: [this.idempotencyKey(task.idempotencyKeyHash), this.taskKey(task.id), this.queueKey()],
      arguments: [task.id, JSON.stringify(task), `${this.prefix}:task:`, String(input.now)],
    })) as [string, string];
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
      keys: [this.queueKey()],
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
      keys: [this.taskKey(taskId), this.queueKey()],
      arguments: [instanceId, JSON.stringify(outcome), String(now)],
    })) as string;
    return raw === "" ? null : parseTask(raw);
  }

  async close(): Promise<void> {
    await this.client.quit();
  }
}
