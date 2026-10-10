// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/httpClient";
import type { BackupPolicyV1 } from "../../api/workspaceBackupApi";
import AutomaticBackupsTab from "./AutomaticBackupsTab";

const listBackupPolicies = vi.fn();
const listBackupTargets = vi.fn();
const listBackupMirrors = vi.fn();
vi.mock("../../api/workspaceBackupApi", () => ({
  listBackupPolicies: (...args: unknown[]) => listBackupPolicies(...args),
  listBackupTargets: (...args: unknown[]) => listBackupTargets(...args),
  listBackupMirrors: (...args: unknown[]) => listBackupMirrors(...args),
}));

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("AutomaticBackupsTab", () => {
  it("shows a real policy while reporting unavailable target and mirror APIs", async () => {
    const policy: BackupPolicyV1 = {
      schemaVersion: 1,
      policyId: "p-process-boundary",
      name: "native process boundary",
      enabled: false,
      schedule: { cron: "0 3 * * *", timezone: "UTC", misfirePolicy: "skip", catchupWindowSeconds: 86400, jitterSeconds: 0 },
      scope: { mode: "full", projectIds: [], includeHistory: true, includeExternalState: true, coveragePolicy: "strict" },
      frontendMirror: { mode: "best-effort", maxAgeSeconds: 3600 },
      protection: { mode: "age-recipient", recipients: ["age1testfixture"] },
      targetId: "managed-local",
      retentionPolicyId: "default",
      retry: { maxAttempts: 3, initialBackoffSeconds: 60, maxBackoffSeconds: 3600 },
      createdAt: "2026-09-05T00:00:00Z",
      updatedAt: "2026-09-05T00:00:00Z",
    };
    listBackupPolicies.mockResolvedValue({ policies: [policy], nextRuns: { [policy.policyId]: null } });
    listBackupTargets.mockRejectedValue(new ApiError("native target route pending", 501));
    listBackupMirrors.mockRejectedValue(new ApiError("native mirror route pending", 501));
    const onError = vi.fn();

    render(<AutomaticBackupsTab onError={onError} onMessage={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("native process boundary")).toBeTruthy());
    expect(screen.queryByText("还没有定时备份策略。")).toBeNull();
    expect(screen.getByText("备份目标列表暂不可用。")).toBeTruthy();
    expect(screen.getByText("best-effort · 状态不可用")).toBeTruthy();
    expect(onError).toHaveBeenCalledWith("备份目标：native target route pending；会话镜像：native mirror route pending");
  });
});
