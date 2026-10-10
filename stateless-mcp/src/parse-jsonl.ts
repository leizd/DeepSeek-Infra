import { createHash } from "node:crypto";

import type { TaskRecord } from "./task-store.js";

export interface JsonlLimits {
  maxTotalBytes: number;
  maxLineBytes: number;
  maxTasks: number;
  maxLogsPerTaskBytes: number;
  maxJsonDepth: number;
}

export const DEFAULT_JSONL_LIMITS: JsonlLimits = {
  maxTotalBytes: 5_000_000_000,
  maxLineBytes: 1024 * 1024,
  maxTasks: 100_000,
  maxLogsPerTaskBytes: 262_144,
  maxJsonDepth: 64,
};

export interface SnapshotMetadataRecord {
  stateGeneration: number;
  restoreEpoch: string;
}

export type SnapshotEntry =
  | { type: "metadata"; schemaVersion: 1; record: SnapshotMetadataRecord }
  | { type: "task"; schemaVersion: 1; task: TaskRecord }
  | { type: "idempotency"; schemaVersion: 1; record: { hash: string; taskId: string; requestHash: string } }
  | { type: "log"; schemaVersion: 1; record: { taskId: string; stdout: string; stderr: string } }
  | { type: "complete"; schemaVersion: 1; record: { stateGeneration: number } };

export class SnapshotParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SnapshotParseError";
  }
}

export interface SnapshotStreamParser {
  entries: AsyncGenerator<SnapshotEntry, void, void>;
  digestHex(): string;
  totalBytes(): number;
}

function assertJsonDepth(value: unknown, depth: number, maxDepth: number): void {
  if (depth > maxDepth) {
    throw new SnapshotParseError("snapshot entry exceeds maximum JSON depth");
  }
  if (Array.isArray(value)) {
    for (const item of value) assertJsonDepth(item, depth + 1, maxDepth);
    return;
  }
  if (typeof value === "object" && value !== null) {
    for (const item of Object.values(value)) assertJsonDepth(item, depth + 1, maxDepth);
  }
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new SnapshotParseError(`snapshot entry is missing ${field}`);
  }
  return value;
}

function requiredGeneration(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
    throw new SnapshotParseError(`snapshot entry has invalid ${field}`);
  }
  return value;
}

export function parseSnapshotStream(
  source: AsyncIterable<string>,
  limits: JsonlLimits = DEFAULT_JSONL_LIMITS,
): SnapshotStreamParser {
  const hash = createHash("sha256");
  let bytes = 0;
  let finished = false;

  async function* entries(): AsyncGenerator<SnapshotEntry, void, void> {
    let buffer = "";
    let sawMetadata: SnapshotMetadataRecord | null = null;
    let sawComplete: number | null = null;
    let tasks = 0;
    const taskIds = new Set<string>();
    const logBytes = new Map<string, number>();

    const handleLine = (line: string): SnapshotEntry => {
      const lineBytes = Buffer.byteLength(line, "utf8") + 1;
      if (lineBytes - 1 > limits.maxLineBytes) {
        throw new SnapshotParseError("snapshot line exceeds maximum size");
      }
      bytes += lineBytes;
      if (bytes > limits.maxTotalBytes) {
        throw new SnapshotParseError("snapshot exceeds maximum total size");
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(line);
      } catch {
        throw new SnapshotParseError("snapshot line is not valid JSON");
      }
      assertJsonDepth(parsed, 1, limits.maxJsonDepth);
      if (typeof parsed !== "object" || parsed === null) {
        throw new SnapshotParseError("snapshot entry must be a JSON object");
      }
      const raw = parsed as Record<string, unknown>;
      if (raw.schemaVersion !== 1) {
        throw new SnapshotParseError("unsupported stateless MCP backup schema");
      }
      if (sawComplete !== null) {
        throw new SnapshotParseError("snapshot has entries after the complete record");
      }
      const type = raw.type;
      if (type === "metadata") {
        if (sawMetadata !== null || taskIds.size > 0) {
          throw new SnapshotParseError("snapshot metadata must be the first entry");
        }
        const record = raw.record ?? raw;
        const metadata = {
          stateGeneration: requiredGeneration((record as Record<string, unknown>).stateGeneration, "stateGeneration"),
          restoreEpoch: requiredString((record as Record<string, unknown>).restoreEpoch, "restoreEpoch"),
        };
        sawMetadata = metadata;
        return { type: "metadata", schemaVersion: 1, record: metadata };
      }
      if (sawMetadata === null) {
        throw new SnapshotParseError("snapshot metadata must be the first entry");
      }
      if (type === "task") {
        const task = raw.task as TaskRecord | undefined;
        if (task === undefined || typeof task !== "object") {
          throw new SnapshotParseError("snapshot task entry is invalid");
        }
        const id = requiredString(task.id, "task.id");
        if (taskIds.has(id)) {
          throw new SnapshotParseError("snapshot contains a duplicate task id");
        }
        tasks += 1;
        if (tasks > limits.maxTasks) {
          throw new SnapshotParseError("snapshot exceeds maximum task count");
        }
        taskIds.add(id);
        return { type: "task", schemaVersion: 1, task };
      }
      if (type === "idempotency") {
        const record = raw.record as Record<string, unknown> | undefined;
        if (record === undefined || typeof record !== "object") {
          throw new SnapshotParseError("snapshot idempotency entry is invalid");
        }
        const parsedRecord = {
          hash: requiredString(record.hash, "idempotency.hash"),
          taskId: requiredString(record.taskId, "idempotency.taskId"),
          requestHash: requiredString(record.requestHash, "idempotency.requestHash"),
        };
        if (!taskIds.has(parsedRecord.taskId)) {
          throw new SnapshotParseError("snapshot idempotency record references an unknown task");
        }
        return { type: "idempotency", schemaVersion: 1, record: parsedRecord };
      }
      if (type === "log") {
        const record = raw.record as Record<string, unknown> | undefined;
        if (record === undefined || typeof record !== "object") {
          throw new SnapshotParseError("snapshot log entry is invalid");
        }
        const parsedRecord = {
          taskId: requiredString(record.taskId, "log.taskId"),
          stdout: typeof record.stdout === "string" ? record.stdout : "",
          stderr: typeof record.stderr === "string" ? record.stderr : "",
        };
        if (!taskIds.has(parsedRecord.taskId)) {
          throw new SnapshotParseError("snapshot log record references an unknown task");
        }
        const size =
          (logBytes.get(parsedRecord.taskId) ?? 0) +
          Buffer.byteLength(parsedRecord.stdout, "utf8") +
          Buffer.byteLength(parsedRecord.stderr, "utf8");
        if (size > limits.maxLogsPerTaskBytes) {
          throw new SnapshotParseError("snapshot task logs exceed maximum size");
        }
        logBytes.set(parsedRecord.taskId, size);
        return { type: "log", schemaVersion: 1, record: parsedRecord };
      }
      if (type === "complete") {
        sawComplete = requiredGeneration(raw.stateGeneration, "complete.stateGeneration");
        if (sawMetadata.stateGeneration !== sawComplete) {
          throw new SnapshotParseError("snapshot metadata and complete generations differ");
        }
        return { type: "complete", schemaVersion: 1, record: { stateGeneration: sawComplete } };
      }
      throw new SnapshotParseError("snapshot entry has an unknown type");
    };

    for await (const chunk of source) {
      hash.update(chunk, "utf8");
      buffer += chunk;
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        if (sawComplete !== null) {
          throw new SnapshotParseError("snapshot has entries after the complete record");
        }
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (line.length === 0 || line === "\r") {
          newline = buffer.indexOf("\n");
          continue;
        }
        yield handleLine(line.endsWith("\r") ? line.slice(0, -1) : line);
        newline = buffer.indexOf("\n");
      }
    }
    if (buffer.length > 0) {
      throw new SnapshotParseError("snapshot ends with a partial line");
    }
    if (sawMetadata === null || sawComplete === null) {
      throw new SnapshotParseError("stateless MCP backup is incomplete");
    }
    finished = true;
  }

  return {
    entries: entries(),
    digestHex(): string {
      if (!finished) {
        throw new SnapshotParseError("snapshot digest is unavailable before the stream completes");
      }
      return hash.digest("hex");
    },
    totalBytes(): number {
      return bytes;
    },
  };
}
