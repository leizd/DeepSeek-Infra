import { httpClient, type HttpClient } from "./httpClient";
import type { Attachment } from "../domain/chat/types";
import { normalizeUploadedFile } from "../features/attachments/attachmentMapper";

export interface ShareTargetPayload {
  prompt: string;
  attachments: Attachment[];
  errors: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function normalizeShareTarget(raw: unknown): ShareTargetPayload {
  const record = isRecord(raw) ? raw : {};
  return {
    prompt: typeof record.prompt === "string" ? record.prompt.trim() : "",
    attachments: Array.isArray(record.attachments)
      ? record.attachments.flatMap((attachment) => {
          if (!isRecord(attachment)) return [];
          const name = typeof attachment.name === "string" && attachment.name ? attachment.name : "附件";
          return [{ ...normalizeUploadedFile({ ...attachment, name }) }];
        })
      : [],
    errors: Array.isArray(record.errors)
      ? record.errors.flatMap((error) => {
          if (!isRecord(error)) return [];
          const message = typeof error.error === "string" ? error.error : "";
          return message ? [message] : [];
        })
      : [],
  };
}

export async function getShareTarget(shareId: string, client: HttpClient = httpClient): Promise<ShareTargetPayload> {
  const body = await client.json<{ share?: unknown }>(`/api/share-target?id=${encodeURIComponent(shareId)}`);
  return normalizeShareTarget(body.share);
}

export function readShareIdFromLocation(search: string): string {
  return new URLSearchParams(search).get("share")?.trim() ?? "";
}

export function stripShareIdFromLocation(pathname: string, search: string, hash: string): string {
  const params = new URLSearchParams(search);
  params.delete("share");
  const query = params.toString();
  return `${pathname}${query ? `?${query}` : ""}${hash}`;
}
