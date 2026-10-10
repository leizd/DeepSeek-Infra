import { httpClient, type HttpClient } from "./httpClient";
import type { JsonRecord } from "../domain/chat/types";

export interface Reminder {
  id: string;
  title: string;
  content: string;
  dueAt: string;
  createdAt: number;
  notified: boolean;
  notifiedAt?: number;
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function normalizeReminder(raw: unknown): Reminder | null {
  if (!isRecord(raw)) return null;
  const id = typeof raw.id === "string" ? raw.id : "";
  const dueAt = typeof raw.dueAt === "string" ? raw.dueAt : "";
  if (!id || !dueAt) return null;
  return {
    id,
    title: typeof raw.title === "string" && raw.title ? raw.title : "提醒",
    content: typeof raw.content === "string" ? raw.content : "",
    dueAt,
    createdAt: typeof raw.createdAt === "number" ? raw.createdAt : 0,
    notified: raw.notified === true,
    notifiedAt: typeof raw.notifiedAt === "number" ? raw.notifiedAt : undefined,
  };
}

function normalizeReminderList(value: unknown): Reminder[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const reminder = normalizeReminder(item);
    return reminder ? [reminder] : [];
  });
}

async function reminderAction<T>(body: JsonRecord, client: HttpClient): Promise<T> {
  return client.postJson<T>("/api/reminders", body);
}

export async function listReminders(client: HttpClient = httpClient): Promise<Reminder[]> {
  const body = await reminderAction<{ reminders?: unknown }>({ action: "list" }, client);
  return normalizeReminderList(body.reminders);
}

export async function createReminder(
  input: { title: string; content: string; dueAt: string },
  client: HttpClient = httpClient,
): Promise<Reminder> {
  const body = await reminderAction<{ reminder?: unknown }>(
    { action: "create", title: input.title, content: input.content, dueAt: input.dueAt },
    client,
  );
  const reminder = normalizeReminder(body.reminder);
  if (!reminder) throw new Error("提醒创建失败");
  return reminder;
}

export async function deleteReminder(reminderId: string, client: HttpClient = httpClient): Promise<void> {
  await reminderAction({ action: "delete", id: reminderId }, client);
}

export async function fetchDueReminders(client: HttpClient = httpClient): Promise<Reminder[]> {
  const body = await client.postJson<{ reminders?: unknown }>("/api/reminders/due", {});
  return normalizeReminderList(body.reminders);
}
