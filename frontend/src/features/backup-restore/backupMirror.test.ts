import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/httpClient";
import type { FrontendBackupEnvelopeV1, PutBackupMirrorRequest } from "../../api/workspaceBackupApi";
import { WORKSPACE_RESTORE_FENCE_KEY } from "../../domain/conversation/persistence";

const putBackupMirror = vi.fn();
vi.mock("../../api/workspaceBackupApi", () => ({
  putBackupMirror: (...args: unknown[]) => putBackupMirror(...args),
}));

const envelope: FrontendBackupEnvelopeV1 = {
  schemaVersion: 1,
  sourceVersion: "4.4.9",
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
  backupMirrorStorageKeys,
  claimLeadership,
  clientReplicaId,
  freezeBackupMirrorForRestore,
  isLeader,
  onBackupMirrorUploaded,
  resetBackupMirrorStateForTests,
  scheduleBackupMirrorUpload,
  unfreezeBackupMirrorAfterRestore,
  uploadBackupMirror,
  type BackupMirrorEnvironment,
  type BackupMirrorTimers,
  type BroadcastChannelLike,
  type MirrorChannelMessage,
} from "./backupMirror";

function memoryStorage(seed: Array<[string, string]> = []): Storage {
  const data = new Map<string, string>(seed);
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

function storageWithEpoch(epoch: string, extra: Array<[string, string]> = []): Storage {
  return memoryStorage([["deepseek-infra.workspace.active-epoch", epoch], ...extra]);
}

class FakeChannel implements BroadcastChannelLike {
  static peers = new Set<FakeChannel>();
  readonly listeners = new Set<(event: { data: unknown }) => void>();

  constructor() {
    FakeChannel.peers.add(this);
  }

  postMessage(message: unknown): void {
    for (const peer of FakeChannel.peers) {
      if (peer === this) continue;
      peer.listeners.forEach((listener) => listener({ data: message }));
    }
  }

  addEventListener(_type: "message", listener: (event: { data: unknown }) => void): void {
    this.listeners.add(listener);
  }

  removeEventListener(_type: "message", listener: (event: { data: unknown }) => void): void {
    this.listeners.delete(listener);
  }

  close(): void {
    FakeChannel.peers.delete(this);
    this.listeners.clear();
  }
}

/** Timers that never schedule real macrotasks — keeps vitest workers from hanging. */
function silentTimers(): BackupMirrorTimers {
  return {
    setTimeout: () => 0,
    clearTimeout: () => undefined,
    setInterval: () => 0,
    clearInterval: () => undefined,
  };
}

function fakeTimersBridge(): BackupMirrorTimers {
  return {
    setTimeout: (handler, timeout) => setTimeout(handler, timeout) as unknown as number,
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    setInterval: (handler, timeout) => setInterval(handler, timeout) as unknown as number,
    clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
  };
}

function baseEnv(overrides: Partial<BackupMirrorEnvironment> = {}): BackupMirrorEnvironment {
  return {
    storage: storageWithEpoch("epoch-a"),
    sessionStorage: memoryStorage(),
    createBroadcastChannel: () => new FakeChannel(),
    now: () => 1_000_000,
    online: () => true,
    timers: silentTimers(),
    ...overrides,
  };
}

describe("backupMirror", () => {
  beforeEach(() => {
    FakeChannel.peers.clear();
    putBackupMirror.mockReset();
    putBackupMirror.mockResolvedValue({
      schemaVersion: 2,
      profileId: "mirror_x",
      generationId: "gen_aaaaaaaaaaaaaaaaaaaaaaaa",
      sourceEpoch: "epoch-a",
      clientReplicaId: "replica",
      clientSequence: 1,
      envelopeDigest: "abc123",
      recipientSetDigest: "deadbeef",
      conversations: 0,
      conflicts: 0,
      createdAt: "2026-01-01T00:00:00Z",
      acknowledgedAt: "2026-01-01T00:00:00Z",
      ciphertextSha256: "ff".repeat(32),
      creationVerified: true,
    });
    collectFrontendBackupEnvelope.mockReset();
    collectFrontendBackupEnvelope.mockResolvedValue({ ...envelope });
    resetBackupMirrorStateForTests();
  });

  afterEach(() => {
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

  it("elects a leader and only the leader uploads with replica/sequence", async () => {
    const storage = storageWithEpoch("epoch-a");
    const sessionA = memoryStorage();
    const sessionB = memoryStorage();
    const clock = { now: 1_000_000 };
    const factory = () => new FakeChannel();
    const timers = silentTimers();

    const replicaA = clientReplicaId(sessionA);
    const replicaB = clientReplicaId(sessionB);
    expect(replicaA).not.toBe(replicaB);

    expect(claimLeadership(storage, replicaA, {
      now: () => clock.now,
      createBroadcastChannel: factory,
      timers,
      sessionStorage: sessionA,
    })).toBe(true);
    expect(isLeader(storage, replicaA, clock.now)).toBe(true);
    expect(claimLeadership(storage, replicaB, {
      now: () => clock.now,
      createBroadcastChannel: factory,
      timers,
      sessionStorage: sessionB,
    })).toBe(false);

    await uploadBackupMirror("4.4.9", {
      storage,
      sessionStorage: sessionA,
      createBroadcastChannel: factory,
      now: () => clock.now,
      online: () => true,
      timers,
    });
    expect(putBackupMirror).toHaveBeenCalledTimes(1);
    const [, body] = putBackupMirror.mock.calls[0] as [string, PutBackupMirrorRequest];
    expect(body.clientReplicaId).toBe(replicaA);
    expect(body.clientSequence).toBe(1);
    expect(body.sourceEpoch).toBe("epoch-a");
    expect(body.envelope.digest).toBe("abc123");

    putBackupMirror.mockClear();
    // Keep A's live lease; B must not upload.
    storage.setItem(
      backupMirrorStorageKeys.leaderLease,
      JSON.stringify({ schemaVersion: 1, replicaId: replicaA, expiresAt: clock.now + 10_000, claimedAt: clock.now }),
    );
    await uploadBackupMirror("4.4.9", {
      storage,
      sessionStorage: sessionB,
      createBroadcastChannel: factory,
      now: () => clock.now,
      online: () => true,
      timers,
    });
    expect(putBackupMirror).not.toHaveBeenCalled();
  });

  it("lets a follower take over after the leader lease expires", async () => {
    const storage = storageWithEpoch("epoch-a");
    const sessionB = memoryStorage();
    const clock = { now: 5_000 };
    storage.setItem(
      backupMirrorStorageKeys.leaderLease,
      JSON.stringify({ schemaVersion: 1, replicaId: "old-leader", expiresAt: 4_000, claimedAt: 1_000 }),
    );
    await uploadBackupMirror("4.4.9", baseEnv({
      storage,
      sessionStorage: sessionB,
      now: () => clock.now,
    }));
    expect(putBackupMirror).toHaveBeenCalledTimes(1);
    expect(isLeader(storage, clientReplicaId(sessionB), clock.now)).toBe(true);
  });

  it("skips re-uploading an unchanged digest for the same epoch", async () => {
    const env = baseEnv();
    await uploadBackupMirror("4.4.9", env);
    await uploadBackupMirror("4.4.9", env);
    expect(putBackupMirror).toHaveBeenCalledTimes(1);
  });

  it("freezes uploads while a restore fence is active", async () => {
    await uploadBackupMirror("4.4.9", baseEnv({
      storage: storageWithEpoch("epoch-a", [[WORKSPACE_RESTORE_FENCE_KEY, "{}"]]),
    }));
    expect(putBackupMirror).not.toHaveBeenCalled();
    freezeBackupMirrorForRestore();
    unfreezeBackupMirrorAfterRestore();
  });

  it("backs off while offline and uploads after recovery", async () => {
    const online = { value: false };
    let scheduled = 0;
    const timers: BackupMirrorTimers = {
      setTimeout: () => {
        scheduled += 1;
        return scheduled;
      },
      clearTimeout: silentTimers().clearTimeout,
      setInterval: silentTimers().setInterval,
      clearInterval: silentTimers().clearInterval,
    };
    const env = baseEnv({ online: () => online.value, timers });
    await uploadBackupMirror("4.4.9", env);
    expect(putBackupMirror).not.toHaveBeenCalled();
    expect(scheduled).toBeGreaterThan(0);
    online.value = true;
    await uploadBackupMirror("4.4.9", env);
    expect(putBackupMirror).toHaveBeenCalledTimes(1);
  });

  it("retries with a higher sequence after mirror-stale-sequence", async () => {
    putBackupMirror
      .mockRejectedValueOnce(new ApiError("mirror-stale-sequence: clientSequence must increase", 409))
      .mockResolvedValueOnce({
        schemaVersion: 2,
        profileId: "mirror_x",
        generationId: "gen_bbbbbbbbbbbbbbbbbbbbbbbb",
        sourceEpoch: "epoch-a",
        clientSequence: 99,
        envelopeDigest: "abc123",
        recipientSetDigest: "deadbeef",
        conversations: 0,
        conflicts: 0,
        createdAt: "2026-01-01T00:00:00Z",
        acknowledgedAt: "2026-01-01T00:00:00Z",
        ciphertextSha256: "aa".repeat(32),
        creationVerified: true,
      });
    await uploadBackupMirror("4.4.9", baseEnv({ now: () => 50 }));
    expect(putBackupMirror).toHaveBeenCalledTimes(2);
    const second = putBackupMirror.mock.calls[1]?.[1] as PutBackupMirrorRequest;
    expect(second.clientSequence).toBeGreaterThan(1);
  });

  it("broadcasts the generation id after a successful upload", async () => {
    const storage = storageWithEpoch("epoch-a");
    const seen: MirrorChannelMessage[] = [];
    const stop = onBackupMirrorUploaded((message) => seen.push(message));
    await uploadBackupMirror("4.4.9", baseEnv({ storage }));
    stop();
    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({
      type: "mirror_uploaded",
      generationId: "gen_aaaaaaaaaaaaaaaaaaaaaaaa",
      envelopeDigest: "abc123",
      sourceEpoch: "epoch-a",
    });
    const profileId = await backupMirrorProfileId("epoch-a");
    expect(storage.getItem(`${backupMirrorStorageKeys.headGenerationPrefix}${profileId}`)).toBe(
      "gen_aaaaaaaaaaaaaaaaaaaaaaaa",
    );
  });

  it("swallows non-retryable upload failures without throwing", async () => {
    putBackupMirror.mockRejectedValue(new ApiError("bad envelope", 400));
    await expect(uploadBackupMirror("4.4.9", baseEnv())).resolves.toBeUndefined();
  });

  it("debounces scheduled uploads", async () => {
    vi.useFakeTimers();
    try {
      const env = baseEnv({ timers: fakeTimersBridge() });
      scheduleBackupMirrorUpload("4.4.9", 10, env);
      scheduleBackupMirrorUpload("4.4.9", 10, env);
      await vi.advanceTimersByTimeAsync(20);
      expect(collectFrontendBackupEnvelope).toHaveBeenCalledTimes(1);
    } finally {
      resetBackupMirrorStateForTests();
      vi.useRealTimers();
    }
  });

  it("stable replica ids come from session storage", () => {
    const session = memoryStorage();
    const first = clientReplicaId(session);
    const second = clientReplicaId(session);
    expect(first).toBe(second);
    expect(first).toMatch(/^mirror_[0-9a-f]{16,}$/);
  });
});
