import { describe, expect, it, vi } from "vitest";

import { HttpClient } from "./httpClient";
import { createReminder, fetchDueReminders, listReminders, normalizeReminder } from "./remindersApi";

function fakeClient(payload: unknown): { client: HttpClient; fetchImpl: ReturnType<typeof vi.fn> } {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }));
  return { client: new HttpClient({ fetchImpl }), fetchImpl };
}

describe("remindersApi", () => {
  it("normalizes reminders with defaults", () => {
    expect(normalizeReminder({ id: "r1", dueAt: "2026-07-19T09:00:00Z" })).toMatchObject({
      id: "r1",
      title: "提醒",
      content: "",
      notified: false,
    });
    expect(normalizeReminder({ id: "", dueAt: "x" })).toBeNull();
    expect(normalizeReminder("junk")).toBeNull();
  });

  it("lists reminders via the action endpoint", async () => {
    const { client, fetchImpl } = fakeClient({ reminders: [{ id: "r1", dueAt: "2026-07-19T09:00:00Z", title: "开会" }] });
    const reminders = await listReminders(client);
    const [, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ action: "list" });
    expect(reminders[0]).toMatchObject({ id: "r1", title: "开会" });
  });

  it("creates reminders and rejects malformed responses", async () => {
    const { client } = fakeClient({ reminder: { id: "r2", dueAt: "2026-07-19T09:00:00Z" } });
    await expect(createReminder({ title: "t", content: "c", dueAt: "2026-07-19T09:00:00Z" }, client)).resolves.toMatchObject({ id: "r2" });
    const bad = fakeClient({});
    await expect(createReminder({ title: "t", content: "c", dueAt: "x" }, bad.client)).rejects.toThrow("提醒创建失败");
  });

  it("fetches due reminders from the due endpoint", async () => {
    const { client, fetchImpl } = fakeClient({ reminders: [{ id: "r3", dueAt: "2026-07-18T08:00:00Z", notified: true }] });
    const due = await fetchDueReminders(client);
    expect(String((fetchImpl.mock.calls[0] as unknown as [string])[0])).toBe("/api/reminders/due");
    expect(due[0]).toMatchObject({ id: "r3", notified: true });
  });
});
