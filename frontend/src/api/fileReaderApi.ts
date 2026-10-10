import { httpClient, type HttpClient } from "./httpClient";

export const FILE_READER_CHUNK_COUNT = 6;
export const FILE_READER_MAX_CHUNK_COUNT = 12;

export interface FileReference {
  fileId: string;
  projectId?: string;
}

export interface FileReaderWindow {
  chunkStart: number;
  chunkEnd: number;
  chunkCount: number;
  totalChunks: number;
  hasPrevious: boolean;
  hasNext: boolean;
}

export interface FileReaderChunk {
  index: number;
  start: number;
  end: number;
  lineStart: number;
  lineEnd: number;
  text: string;
}

export interface FileReaderFileInfo {
  name: string;
  kind: string;
  type: string;
  size: number;
  charCount: number;
  chunkCount: number;
  pageCount: number;
  fileId: string;
  projectId: string;
  sourceAvailable: boolean;
}

export interface FileReaderResponse {
  file: FileReaderFileInfo;
  window: FileReaderWindow;
  chunks: FileReaderChunk[];
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function normalizeChunk(raw: unknown): FileReaderChunk {
  const record = asRecord(raw);
  return {
    index: asNumber(record.index),
    start: asNumber(record.start),
    end: asNumber(record.end),
    lineStart: asNumber(record.lineStart),
    lineEnd: asNumber(record.lineEnd),
    text: asString(record.text),
  };
}

function normalizeWindow(raw: unknown, fallbackChunkCount: number, fallbackChunkStart: number): FileReaderWindow {
  const record = asRecord(raw);
  return {
    chunkStart: Math.max(1, asNumber(record.chunkStart, fallbackChunkStart)),
    chunkEnd: asNumber(record.chunkEnd),
    chunkCount: asNumber(record.chunkCount, fallbackChunkCount),
    totalChunks: asNumber(record.totalChunks),
    hasPrevious: record.hasPrevious === true,
    hasNext: record.hasNext === true,
  };
}

function normalizeFileInfo(raw: unknown, ref: FileReference): FileReaderFileInfo {
  const record = asRecord(raw);
  return {
    name: asString(record.name, "附件"),
    kind: asString(record.kind, "text"),
    type: asString(record.type),
    size: asNumber(record.size),
    charCount: asNumber(record.charCount),
    chunkCount: asNumber(record.chunkCount),
    pageCount: asNumber(record.pageCount),
    fileId: asString(record.fileId, ref.fileId),
    projectId: asString(record.projectId, ref.projectId ?? ""),
    sourceAvailable: record.sourceAvailable !== false,
  };
}

export async function loadFileReaderWindow(
  ref: FileReference,
  chunkStart: number,
  chunkCount: number = FILE_READER_CHUNK_COUNT,
  client: HttpClient = httpClient,
): Promise<FileReaderResponse> {
  const boundedChunkCount = Math.min(FILE_READER_MAX_CHUNK_COUNT, Math.max(1, Math.round(chunkCount)));
  const boundedChunkStart = Math.max(1, Math.round(chunkStart));
  const body = await client.postJson<Record<string, unknown>>("/api/file-reader", {
    fileId: ref.fileId,
    projectId: ref.projectId ?? "",
    chunkStart: boundedChunkStart,
    chunkCount: boundedChunkCount,
  });
  return {
    file: normalizeFileInfo(body.file, ref),
    window: normalizeWindow(body.window, boundedChunkCount, boundedChunkStart),
    chunks: Array.isArray(body.chunks) ? body.chunks.map(normalizeChunk) : [],
  };
}

export interface FilePageText {
  index: number;
  pageCount: number;
  text: string;
  hasText: boolean;
}

export interface FileChunkResult {
  file: {
    name: string;
    kind: string;
    fileId: string;
    projectId: string;
  };
  index: number;
  text: string;
}

export async function loadFileChunk(
  ref: FileReference,
  chunkIndex: number,
  client: HttpClient = httpClient,
): Promise<FileChunkResult> {
  const boundedChunkIndex = Math.max(1, Math.round(chunkIndex));
  const body = await client.postJson<Record<string, unknown>>("/api/file-chunk", {
    fileId: ref.fileId,
    projectId: ref.projectId ?? "",
    chunkIndex: boundedChunkIndex,
  });
  const file = asRecord(body.file);
  const chunk = asRecord(body.chunk);
  return {
    file: {
      name: asString(file.name, "附件"),
      kind: asString(file.kind, "text"),
      fileId: asString(file.fileId, ref.fileId),
      projectId: asString(file.projectId, ref.projectId ?? ""),
    },
    index: asNumber(chunk.index, boundedChunkIndex),
    text: asString(chunk.text),
  };
}

export async function loadFilePageText(
  ref: FileReference,
  page: number,
  client: HttpClient = httpClient,
): Promise<FilePageText> {
  const body = await client.postJson<Record<string, unknown>>("/api/file-page-text", {
    fileId: ref.fileId,
    projectId: ref.projectId ?? "",
    page: Math.max(1, Math.round(page)),
  });
  const record = asRecord(body.page);
  return {
    index: asNumber(record.index, page),
    pageCount: asNumber(record.pageCount),
    text: asString(record.text),
    hasText: record.hasText === true,
  };
}

function referenceParams(ref: FileReference): URLSearchParams {
  const params = new URLSearchParams({ fileId: ref.fileId });
  if (ref.projectId) params.set("projectId", ref.projectId);
  return params;
}

export function fileSourceUrl(ref: FileReference, download = false): string {
  const params = referenceParams(ref);
  if (download) params.set("download", "1");
  return `/api/file-source?${params.toString()}`;
}

export function filePageImageUrl(ref: FileReference, page: number, scale = 1.6): string {
  const params = referenceParams(ref);
  params.set("page", String(Math.max(1, Math.round(page))));
  params.set("scale", String(Math.min(3, Math.max(0.3, scale))));
  return `/api/file-page-image?${params.toString()}`;
}
