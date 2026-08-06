import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FrontendBackupEnvelopeV1 } from "../../api/workspaceBackupApi";

const putBackupMirror = vi.fn();
vi.mock("../../api/workspaceBackupApi", () => ({
  putBackupMirror: (...args: unknown[]) => putBackupMirror(...args),
}));

const envelope: FrontendBackupEnvelopeV1 = {
  schemaVersion: 1,
  sourceVersion: "4.4.4",
  createdAt: 1,
  conversations: [],
  conflicts: [],
  digest: "abc123",
};

const collectFrontendBackupEnvelope = vi.fn();
vi.mock("./frontendBackup", () => ({
  collectFrontendBackupEnvelope: (...args: unknown[]) => collectFrontendBackupEnvelope(...args),
}));

import {
  backupMirrorProfileId,
  resetBackupMirrorStateForTests,
  scheduleBackupMirrorUpload,
  uploadBackupMirror,
} from "./backupMirror";

function storageWithEpoch(epoch: string): Storage {
  const data = new Map<string, string>([["deepseek-infra.workspace.active-epoch", epoch]]);
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    removeItem: (key: string) => void data.delete(key),
    clear: () => data.clear(),
    key: (index: number) => [...data.keys()][index] ?? null,
    get length() {
      return data.size;
    },
  } as Storage;
}

describe("backupMirror", () => {
  beforeEach(() => {
    putBackupMirror.mockReset();
    putBackupMirror.mockResolvedValue({});
    collectFrontendBackupEnvelope.mockReset();
    collectFrontendBackupEnvelope.mockResolvedValue({ ...envelope });
    resetBackupMirrorStateForTests();
  });

  it("derives a deterministic profile id per epoch", async () => {
    const first = await backupMirrorProfileId("epoch-a");
    const second = await backupMirrorProfileId("epoch-a");
    const other = await backupMirrorProfileId("epoch-b");
    expect(first).toBe(second);
    expect(first).toMatch(/^mirror_[0-9a-f]{16}$/);
    expect(first).not.toBe(other);
  });

  it("uploads the collected envelope to its epoch profile", async () => {
    await uploadBackupMirror("4.4.4", storageWithEpoch("epoch-a"));
    expect(putBackupMirror).toHaveBeenCalledTimes(1);
    const [profileId, body] = putBackupMirror.mock.calls[0] as [string, { sourceEpoch: string; envelope: FrontendBackupEnvelopeV1 }];
    expect(profileId).toBe(await backupMirrorProfileId("epoch-a"));
    expect(body.sourceEpoch).toBe("epoch-a");
    expect(body.envelope.digest).toBe("abc123");
  });

  it("skips re-uploading an unchanged digest", async () => {
    const storage = storageWithEpoch("epoch-a");
    await uploadBackupMirror("4.4.4", storage);
    await uploadBackupMirror("4.4.4", storage);
    expect(putBackupMirror).toHaveBeenCalledTimes(1);
  });

  it("swallows upload failures", async () => {
    putBackupMirror.mockRejectedValue(new Error("offline"));
    await expect(uploadBackupMirror("4.4.4", storageWithEpoch("epoch-a"))).resolves.toBeUndefined();
  });

  it("debounces scheduled uploads", async () => {
    vi.useFakeTimers();
    try {
      const storage = storageWithEpoch("epoch-a");
      scheduleBackupMirrorUpload("4.4.4", 10, storage);
      scheduleBackupMirrorUpload("4.4.4", 10, storage);
      await vi.advanceTimersByTimeAsync(20);
      expect(collectFrontendBackupEnvelope).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
