// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/httpClient";
import type { BackupPolicyV1 } from "../../api/workspaceBackupApi";
import AutomaticBackupsTab from "./AutomaticBackupsTab";

const listBackupPolicies = vi.fn();
const listBackupTargets = vi.fn();
const listBackupMirrors = vi.fn();
const createBackupTarget = vi.fn();
vi.mock("../../api/workspaceBackupApi", () => ({
  listBackupPolicies: (...args: unknown[]) => listBackupPolicies(...args),
  listBackupTargets: (...args: unknown[]) => listBackupTargets(...args),
  listBackupMirrors: (...args: unknown[]) => listBackupMirrors(...args),
  createBackupTarget: (...args: unknown[]) => createBackupTarget(...args),
}));

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("AutomaticBackupsTab", () => {
  it("reuses an unconfirmed filesystem request and assigns a new key when the form changes", async () => {
    listBackupPolicies.mockResolvedValue({ policies: [], nextRuns: {} });
    listBackupTargets.mockResolvedValue({ targets: [], health: [] });
    listBackupMirrors.mockResolvedValue({ mirrors: [] });
    createBackupTarget.mockRejectedValue(new TypeError("lost target response"));
    const onError = vi.fn();
    render(<AutomaticBackupsTab onError={onError} onMessage={vi.fn()} />);
    await waitFor(() => expect(onError).toHaveBeenCalledWith(""));
    const path = screen.getByPlaceholderText(/^目标目录绝对路径/);
    const label = screen.getByPlaceholderText("标签（可选）");
    const register = screen.getByRole<HTMLButtonElement>("button", { name: "注册目标" });
    fireEvent.change(path, { target: { value: " /mounted/disk " } });
    fireEvent.change(label, { target: { value: "Archive" } });

    async function submit(expectedCalls: number) {
      fireEvent.click(register);
      await waitFor(() => {
        expect(createBackupTarget).toHaveBeenCalledTimes(expectedCalls);
        expect(register.disabled).toBe(false);
      });
      return createBackupTarget.mock.calls[expectedCalls - 1][0];
    }

    const first = await submit(1);
    expect(first).toEqual({ path: "/mounted/disk", label: "Archive", operationId: expect.stringMatching(/^target-inspect-public-v1-[a-f0-9]{32}$/) });
    expect(await submit(2)).toEqual(first);
    fireEvent.change(label, { target: { value: "Another archive" } });
    const changed = await submit(3);
    expect(changed.label).toBe("Another archive");
    expect(changed.operationId).not.toBe(first.operationId);
  });

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
