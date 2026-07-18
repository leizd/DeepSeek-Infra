import { httpClient, type HttpClient } from "./httpClient";
import type { JsonRecord } from "../domain/chat/types";

export interface Skill {
  skillId: string;
  name: string;
  description: string;
  version: string;
  systemPrompt: string;
  builtin: boolean;
  disabled: boolean;
  updatedAt: string;
}

export interface ProjectSkillBinding {
  enabledSkills: string[];
  defaultSkill: string;
  recentSkills: string[];
  enabledPacks: string[];
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function normalizeSkill(raw: unknown): Skill | null {
  if (!isRecord(raw)) return null;
  const skillId = asString(raw.skillId);
  if (!skillId) return null;
  return {
    skillId,
    name: asString(raw.name, skillId),
    description: asString(raw.description),
    version: asString(raw.version, "1.0.0"),
    systemPrompt: asString(raw.systemPrompt),
    builtin: raw.builtin === true,
    disabled: raw.disabled === true,
    updatedAt: asString(raw.updatedAt),
  };
}

export function normalizeProjectSkillBinding(raw: unknown): ProjectSkillBinding {
  const record = isRecord(raw) ? raw : {};
  return {
    enabledSkills: asStringArray(record.enabledSkills),
    defaultSkill: asString(record.defaultSkill),
    recentSkills: asStringArray(record.recentSkills),
    enabledPacks: asStringArray(record.enabledPacks),
  };
}

async function skillAction<T>(body: JsonRecord, client: HttpClient): Promise<T> {
  return client.postJson<T>("/api/skills", body);
}

export async function listSkills(client: HttpClient = httpClient): Promise<Skill[]> {
  const body = await skillAction<{ skills?: unknown }>({ action: "list", includeDisabled: true }, client);
  if (!Array.isArray(body.skills)) return [];
  return body.skills.flatMap((skill) => {
    const normalized = normalizeSkill(skill);
    return normalized ? [normalized] : [];
  });
}

export async function setSkillDisabled(skillId: string, disabled: boolean, client: HttpClient = httpClient): Promise<void> {
  await skillAction({ action: disabled ? "disable" : "enable", skillId }, client);
}

export async function deleteSkill(skillId: string, client: HttpClient = httpClient): Promise<void> {
  await skillAction({ action: "delete", skillId }, client);
}

export interface SimpleSkillDraft {
  name: string;
  description: string;
  systemPrompt: string;
}

export function buildSimpleSkillConfig(draft: SimpleSkillDraft, existingId?: string): JsonRecord {
  const skillId = existingId ?? `custom-${Date.now().toString(36)}`;
  return {
    skillId,
    name: draft.name.trim().slice(0, 120),
    description: draft.description.trim().slice(0, 600),
    version: "1.0.0",
    systemPrompt: draft.systemPrompt.trim().slice(0, 20_000),
    inputSchema: { type: "object", properties: {} },
    outputSchema: { type: "object", properties: {} },
    allowedTools: [],
    memoryPolicy: { scope: "none", read: false, write: false },
    artifactPolicy: { autoSave: false, types: [] },
    projectBinding: { enabled: false },
  };
}

export async function createSkill(draft: SimpleSkillDraft, client: HttpClient = httpClient): Promise<Skill> {
  const body = await skillAction<{ skill?: unknown }>(
    { action: "create", skill: buildSimpleSkillConfig(draft), overwrite: false },
    client,
  );
  const skill = normalizeSkill(body.skill);
  if (!skill) throw new Error("技能创建失败");
  return skill;
}

export async function updateSkillPrompt(draft: SimpleSkillDraft & { skillId: string }, client: HttpClient = httpClient): Promise<Skill> {
  const body = await skillAction<{ skill?: unknown }>(
    {
      action: "update",
      skillId: draft.skillId,
      patch: {
        name: draft.name.trim().slice(0, 120),
        description: draft.description.trim().slice(0, 600),
        systemPrompt: draft.systemPrompt.trim().slice(0, 20_000),
      },
    },
    client,
  );
  const skill = normalizeSkill(body.skill);
  if (!skill) throw new Error("技能保存失败");
  return skill;
}

export async function fetchProjectSkillBinding(projectId: string, client: HttpClient = httpClient): Promise<ProjectSkillBinding> {
  const body = await client.json<{ skills?: unknown }>(`/api/workspace/projects/${encodeURIComponent(projectId)}/skills`);
  return normalizeProjectSkillBinding(body.skills);
}

export async function saveProjectSkillBinding(
  projectId: string,
  binding: { enabledSkills: readonly string[]; defaultSkill: string },
  client: HttpClient = httpClient,
): Promise<ProjectSkillBinding> {
  const body = await client.json<{ skills?: unknown }>(`/api/workspace/projects/${encodeURIComponent(projectId)}/skills`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabledSkills: [...binding.enabledSkills], defaultSkill: binding.defaultSkill }),
  });
  return normalizeProjectSkillBinding(body.skills);
}
