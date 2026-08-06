import { putBackupMirror } from "../../api/workspaceBackupApi";
import { readActiveWorkspaceEpoch } from "../../domain/conversation/persistence";
import { collectFrontendBackupEnvelope } from "./frontendBackup";

const DEFAULT_SOURCE_VERSION = "4.4.4";
const DEBOUNCE_MS = 2000;

let inFlight: Promise<void> | null = null;
let scheduledHandle: ReturnType<typeof setTimeout> | null = null;
let lastUploadedDigest = "";

function defaultStorage(): Storage {
  const globalWindow = (globalThis as { window?: Window }).window;
  if (!globalWindow) throw new Error("备份镜像需要浏览器环境");
  return globalWindow.localStorage;
}

export async function backupMirrorProfileId(epoch: string): Promise<string> {
  const bytes = new TextEncoder().encode(`frontend-mirror:${epoch}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hex = [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, "0")).join("");
  return `mirror_${hex.slice(0, 16)}`;
}

export async function uploadBackupMirror(
  sourceVersion: string = DEFAULT_SOURCE_VERSION,
  storage: Storage = defaultStorage(),
): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const epoch = readActiveWorkspaceEpoch(storage);
      const envelope = await collectFrontendBackupEnvelope(sourceVersion, false, storage);
      if (envelope.digest === lastUploadedDigest) return;
      const profileId = await backupMirrorProfileId(epoch);
      await putBackupMirror(profileId, {
        sourceEpoch: epoch,
        acknowledgedAt: new Date().toISOString(),
        envelope,
      });
      lastUploadedDigest = envelope.digest;
    } catch (error) {
      // 镜像是尽力而为的后台通道；失败只记录，不影响聊天持久化。
      console.warn("备份镜像上传失败", error);
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

export function scheduleBackupMirrorUpload(
  sourceVersion: string = DEFAULT_SOURCE_VERSION,
  delayMs: number = DEBOUNCE_MS,
  storage?: Storage,
): void {
  if (scheduledHandle !== null) clearTimeout(scheduledHandle);
  scheduledHandle = setTimeout(() => {
    scheduledHandle = null;
    void uploadBackupMirror(sourceVersion, storage ?? defaultStorage());
  }, delayMs);
}

export function resetBackupMirrorStateForTests(): void {
  lastUploadedDigest = "";
  if (scheduledHandle !== null) {
    clearTimeout(scheduledHandle);
    scheduledHandle = null;
  }
}
