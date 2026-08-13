import { describe, expect, it, vi } from "vitest";

import { HttpClient } from "./httpClient";
import {
  abortWorkspaceRestore,
  getDisasterRecoveryStatus,
  pauseWorkspaceRestore,
  preflightWorkspaceRestore,
  resumeWorkspaceRestore,
} from "./workspaceBackupApi";

function fakeClient() {
  const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ restoreId: "restore/1", phase: "paused" }), { status: 200 }));
  return { client: new HttpClient({ fetchImpl }), fetchImpl };
}

describe("workspace recovery job controls", () => {
  it.each([
    [pauseWorkspaceRestore, "pause"],
    [resumeWorkspaceRestore, "resume"],
    [abortWorkspaceRestore, "abort"],
    [preflightWorkspaceRestore, "preflight"],
  ] as const)("posts the durable %s intent", async (operation, action) => {
    const { client, fetchImpl } = fakeClient();

    await operation("restore/1", client);

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/workspace/restores/restore%2F1/${action}`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({});
  });

  it("gets server-derived disaster recovery readiness", async () => {
    const { client, fetchImpl } = fakeClient();

    await getDisasterRecoveryStatus(client);

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/workspace/disaster-recovery/status");
    expect(init.method).toBeUndefined();
    expect(init.body).toBeUndefined();
  });
});
