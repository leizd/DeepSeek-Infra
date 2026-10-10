import type { Attachment, JsonRecord } from "../../domain/chat/types";
import type { UploadLimits } from "../../api/fileUploadApi";

export const MAX_PENDING_ATTACHMENTS = 5;
export const MAX_ATTACHMENT_PROMPT_CHARS = 120_000;

export const ATTACHMENT_ACCEPT =
  ".txt,.md,.markdown,.csv,.tsv,.json,.jsonl,.yaml,.yml,.xml,.html,.htm,.css,.js,.mjs,.cjs,.ts,.tsx,.jsx,.py,.java,.c,.cpp,.h,.hpp,.cs,.go,.rs,.php,.rb,.swift,.kt,.sql,.sh,.ps1,.bat,.log,.ini,.toml,.env,.rtf,.docx,.xlsx,.pptx,.epub,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff,.gif,text/plain,text/markdown,text/csv,application/json,application/pdf,application/rtf,image/*,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.presentationml.presentation,application/epub+zip";

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif"]);
const OCR_ERROR_PATTERN = /ocr|scanned|image-only|扫描/i;

export interface FileRejection {
  name: string;
  error: string;
}

function asRecord(value: unknown): JsonRecord | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonRecord) : null;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" && value ? value : fallback;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function isImageFile(name: string, mimeType = ""): boolean {
  return mimeType.startsWith("image/") || IMAGE_EXTENSIONS.has(fileExtension(name));
}

export function guessKind(name: string, mimeType = ""): string {
  if (isImageFile(name, mimeType)) return "image";
  const extension = fileExtension(name);
  return extension || "text";
}

export function formatBytes(size: number | undefined): string {
  const bytes = typeof size === "number" && Number.isFinite(size) ? Math.max(0, size) : 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function normalizeUploadedFile(raw: JsonRecord): Attachment {
  const name = asString(raw.name, "附件").slice(0, 180);
  const text = asString(raw.text);
  return {
    name,
    type: asString(raw.type),
    kind: asString(raw.kind, "text"),
    size: asNumber(raw.size),
    fileId: asString(raw.fileId) || undefined,
    projectId: asString(raw.projectId) || undefined,
    sourceAvailable: raw.sourceAvailable !== false,
    preview: asString(raw.preview) || text,
    text,
    pageCount: asNumber(raw.pageCount),
    charCount: asNumber(raw.charCount),
    chunkCount: asNumber(raw.chunkCount),
    chunked: raw.chunked === true,
    truncated: raw.truncated === true,
  };
}

export function normalizeUploadError(raw: unknown, fallbackName: string): FileRejection {
  const record = asRecord(raw);
  return {
    name: asString(record?.name, fallbackName),
    error: friendlyUploadError(asString(record?.error), asString(record?.code) || undefined, asNumber(record?.status)),
  };
}

export function friendlyUploadError(message: string, code?: string, status?: number): string {
  if (status === 413 || code === "upload_too_large" || code === "file_too_large") {
    return "文件超过大小限制，请压缩后再试";
  }
  if (code === "unsupported_file" || status === 415) {
    return "不支持的文件类型";
  }
  if (message && OCR_ERROR_PATTERN.test(message)) {
    return "该文件需要 OCR 识别，请重试或检查 OCR 配置";
  }
  return message || "文件识别失败，请重试";
}

export function isOcrRetryable(error: string | undefined): boolean {
  return Boolean(error && OCR_ERROR_PATTERN.test(error));
}

export function validateUploadFiles(
  files: readonly File[],
  limits: UploadLimits,
  slotsAvailable: number,
): { accepted: File[]; rejected: FileRejection[] } {
  const accepted: File[] = [];
  const rejected: FileRejection[] = [];
  let acceptedBytes = 0;
  const maxCount = Math.min(limits.maxFiles, Math.max(0, slotsAvailable));

  files.forEach((file) => {
    const name = file.name || "upload";
    if (file.size > limits.fileMaxBytes) {
      rejected.push({ name, error: `超过单文件大小限制（${formatBytes(limits.fileMaxBytes)}）` });
      return;
    }
    if (accepted.length >= maxCount) {
      rejected.push({ name, error: `附件数量超出上限（最多 ${maxCount} 个）` });
      return;
    }
    if (acceptedBytes + file.size > limits.requestMaxBytes) {
      rejected.push({ name, error: `超过单次上传总大小限制（${formatBytes(limits.requestMaxBytes)}）` });
      return;
    }
    acceptedBytes += file.size;
    accepted.push(file);
  });

  return { accepted, rejected };
}

export function toApiAttachments(
  attachments: readonly Attachment[],
  options: { includeImages?: boolean } = {},
): JsonRecord[] {
  const result: JsonRecord[] = [];
  for (const attachment of attachments) {
    const record: JsonRecord = {
      fileId: attachment.fileId ?? "",
      projectId: attachment.projectId ?? "",
      name: attachment.name,
      type: attachment.type ?? "",
      size: attachment.size ?? 0,
      kind: attachment.kind ?? "text",
      charCount: attachment.charCount ?? 0,
      chunkCount: attachment.chunkCount ?? 0,
      text: attachment.fileId ? "" : (attachment.text ?? ""),
    };
    if (options.includeImages) {
      const imageData = attachment.imagePreview || attachment.thumbnail || "";
      if (imageData && (attachment.kind === "image" || isImageFile(attachment.name, attachment.type ?? ""))) {
        record.imageData = imageData;
      }
    }
    if (record.fileId || record.text || record.imageData) result.push(record);
  }
  return result;
}

export function formatAttachmentsForPrompt(
  attachments: readonly Attachment[],
  maxChars: number = MAX_ATTACHMENT_PROMPT_CHARS,
): string {
  let used = 0;
  const lines = ["[用户上传的文件内容]"];
  let index = 0;
  for (const attachment of attachments) {
    if (!attachment.text || attachment.fileId) continue;
    index += 1;
    const header = `\n--- 文件 ${index}: ${attachment.name} (${formatBytes(attachment.size)}) ---`;
    const remaining = maxChars - used;
    if (remaining <= 0) {
      lines.push("\n[其余附件内容因长度限制未发送]");
      break;
    }
    const text = attachment.text.slice(0, remaining);
    used += text.length;
    lines.push(header, text);
    if (attachment.truncated) lines.push("[文件内容较长，已截断]");
  }
  return lines.join("\n");
}

export function hasInlineAttachmentText(attachments: readonly Attachment[]): boolean {
  return attachments.some((attachment) => attachment.text && !attachment.fileId);
}
