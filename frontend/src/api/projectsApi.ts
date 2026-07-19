import { httpClient, type HttpClient } from "./httpClient";
import type { JsonRecord } from "../domain/chat/types";

export interface ProjectDocument {
  id: string;
  name: string;
  type: string;
  size: number;
  kind: string;
  fileId: string;
  projectId: string;
  sourceAvailable: boolean;
  preview: string;
  pageCount: number;
  charCount: number;
  chunkCount: number;
  chunked: boolean;
  createdAt: number;
}

export interface Project {
  id: string;
  name: string;
  documents: ProjectDocument[];
  createdAt: number;
  updatedAt: number;
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function normalizeProjectDocument(raw: unknown): ProjectDocument | null {
  if (!isRecord(raw)) return null;
  const fileId = asString(raw.fileId);
  if (!fileId) return null;
  return {
    id: asString(raw.id) || fileId,
    name: asString(raw.name, "附件"),
    type: asString(raw.type),
    size: asNumber(raw.size),
    kind: asString(raw.kind, "text"),
    fileId,
    projectId: asString(raw.projectId),
    sourceAvailable: raw.sourceAvailable !== false,
    preview: asString(raw.preview),
    pageCount: asNumber(raw.pageCount),
    charCount: asNumber(raw.charCount),
    chunkCount: asNumber(raw.chunkCount),
    chunked: raw.chunked === true,
    createdAt: asNumber(raw.createdAt),
  };
}

export function normalizeProject(raw: unknown): Project | null {
  if (!isRecord(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  return {
    id,
    name: asString(raw.name, "未命名项目"),
    documents: Array.isArray(raw.documents)
      ? raw.documents.flatMap((document) => {
          const normalized = normalizeProjectDocument(document);
          return normalized ? [normalized] : [];
        })
      : [],
    createdAt: asNumber(raw.createdAt),
    updatedAt: asNumber(raw.updatedAt),
  };
}

async function projectAction<T>(body: JsonRecord, client: HttpClient, init: RequestInit = {}): Promise<T> {
  return client.postJson<T>("/api/projects", body, init);
}

export async function listProjects(init: RequestInit = {}, client: HttpClient = httpClient): Promise<Project[]> {
  const body = await projectAction<{ projects?: unknown }>({ action: "list" }, client, init);
  if (!Array.isArray(body.projects)) return [];
  return body.projects.flatMap((project) => {
    const normalized = normalizeProject(project);
    return normalized ? [normalized] : [];
  });
}

export async function createProject(name: string, client: HttpClient = httpClient): Promise<Project> {
  const body = await projectAction<{ project?: unknown }>({ action: "create", name }, client);
  const project = normalizeProject(body.project);
  if (!project) throw new Error("项目创建失败");
  return project;
}

export async function deleteProject(projectId: string, client: HttpClient = httpClient): Promise<void> {
  await projectAction({ action: "delete", id: projectId }, client);
}

export async function renameProject(projectId: string, name: string, client: HttpClient = httpClient): Promise<Project> {
  const body = await projectAction<{ project?: unknown }>({ action: "rename", id: projectId, name }, client);
  const project = normalizeProject(body.project);
  if (!project) throw new Error("项目重命名失败");
  return project;
}

export async function uploadProjectFiles(
  projectId: string,
  files: readonly File[],
  options: { ocrEnabled?: boolean; apiKey?: string } = {},
  client: HttpClient = httpClient,
): Promise<ProjectDocument[]> {
  const formData = new FormData();
  for (const file of files) formData.append("files", file, file.name || "upload");
  if (options.ocrEnabled) formData.append("ocrEnabled", "1");
  if (options.apiKey) formData.append("apiKey", options.apiKey);
  const body = await client.json<{ documents?: unknown }>(
    `/api/project-files?projectId=${encodeURIComponent(projectId)}`,
    { method: "POST", body: formData },
  );
  if (!Array.isArray(body.documents)) return [];
  return body.documents.flatMap((document) => {
    const normalized = normalizeProjectDocument(document);
    return normalized ? [normalized] : [];
  });
}
