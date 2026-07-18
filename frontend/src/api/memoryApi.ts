import { ApiError, httpClient, type HttpClient } from "./httpClient";
import type { JsonRecord } from "../domain/chat/types";

export const MEMORY_CATEGORIES = ["preference", "project", "todo", "fact"] as const;
export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];

export interface MemoryEntry {
  id: string;
  content: string;
  category: string;
  scope: string;
  pinned: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface MemoryConflict {
  id: string;
  content: string;
  category: string;
  scope: string;
  reason: string;
}

export class MemoryConflictError extends Error {
  readonly conflicts: MemoryConflict[];

  constructor(message: string, conflicts: MemoryConflict[]) {
    super(message);
    this.name = "MemoryConflictError";
    this.conflicts = conflicts;
  }
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function normalizeMemoryEntry(raw: unknown): MemoryEntry | null {
  if (!isRecord(raw)) return null;
  const id = asString(raw.id) || asString(raw.memoryId);
  const content = asString(raw.content);
  if (!id || !content) return null;
  return {
    id,
    content,
    category: asString(raw.category) || asString(raw.type, "fact"),
    scope: asString(raw.scope, "global"),
    pinned: raw.pinned === true,
    createdAt: asString(raw.createdAt),
    updatedAt: asString(raw.updatedAt),
  };
}

export function normalizeMemoryScope(value: string): string {
  const scope = value.trim();
  if (!scope || scope === "global") return "global";
  return /^(project|seek|skill|automation):[\w:-]+$/.test(scope) ? scope : "global";
}

export function normalizeMemorySuggestion(raw: unknown): { content: string; category: MemoryCategory; scope: string } | null {
  if (!isRecord(raw)) return null;
  const content = asString(raw.content).trim().slice(0, 1_200);
  if (!content) return null;
  const category = asString(raw.category);
  return {
    content,
    category: MEMORY_CATEGORIES.includes(category as MemoryCategory) ? (category as MemoryCategory) : "fact",
    scope: normalizeMemoryScope(asString(raw.scope, "global")),
  };
}

export async function listMemories(client: HttpClient = httpClient): Promise<MemoryEntry[]> {
  const body = await client.json<{ memories?: unknown }>("/api/memory");
  if (!Array.isArray(body.memories)) return [];
  return body.memories.flatMap((memory) => {
    const normalized = normalizeMemoryEntry(memory);
    return normalized ? [normalized] : [];
  });
}

async function memoryAction<T>(body: JsonRecord, client: HttpClient): Promise<T> {
  return client.postJson<T>("/api/memory", body);
}

export async function addMemory(
  input: { content: string; category?: string; scope?: string; replaceIds?: readonly string[] },
  client: HttpClient = httpClient,
): Promise<MemoryEntry> {
  try {
    const body = await memoryAction<{ memory?: unknown }>(
      {
        action: "add",
        content: input.content,
        category: input.category ?? "fact",
        scope: normalizeMemoryScope(input.scope ?? "global"),
        ...(input.replaceIds?.length ? { replaceIds: [...input.replaceIds] } : {}),
      },
      client,
    );
    const memory = normalizeMemoryEntry(body.memory);
    if (!memory) throw new Error("记忆保存失败");
    return memory;
  } catch (reason) {
    if (reason instanceof ApiError) {
      const payload = reason.payload;
      if (asString(payload.code).toLowerCase().includes("conflict") && Array.isArray(payload.conflicts)) {
        const conflicts = payload.conflicts.flatMap((raw) => {
          if (!isRecord(raw)) return [];
          return [{
            id: asString(raw.id),
            content: asString(raw.content),
            category: asString(raw.category),
            scope: asString(raw.scope),
            reason: asString(raw.reason),
          }];
        });
        throw new MemoryConflictError(asString(payload.error, "记忆与现有内容冲突"), conflicts);
      }
    }
    throw reason;
  }
}

export async function deleteMemory(memoryId: string, client: HttpClient = httpClient): Promise<void> {
  await memoryAction({ action: "deleteById", id: memoryId }, client);
}

export async function clearMemories(client: HttpClient = httpClient): Promise<void> {
  await memoryAction({ action: "clear" }, client);
}
