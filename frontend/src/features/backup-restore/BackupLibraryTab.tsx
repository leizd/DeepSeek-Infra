import { useCallback, useEffect, useState } from "react";

import {
  getBackupCatalog,
  pinCatalogBackup,
  scrubCatalogBackup,
  verifyUnlockCatalogBackup,
  type BackupCatalogView,
} from "../../api/workspaceBackupApi";

interface Props {
  onError: (message: string) => void;
  onMessage: (message: string) => void;
  formatBytes: (value: number) => string;
}

export default function BackupLibraryTab({ onError, onMessage, formatBytes }: Props) {
  const [view, setView] = useState<BackupCatalogView | null>(null);
  const [busy, setBusy] = useState(false);
  const [drillFor, setDrillFor] = useState("");
  const [identity, setIdentity] = useState("");

  const refresh = useCallback(async () => {
    setView(await getBackupCatalog());
  }, []);

  useEffect(() => {
    void refresh().catch((reason: unknown) => onError(reason instanceof Error ? reason.message : "加载备份库失败"));
  }, [refresh, onError]);

  async function togglePin(backupId: string, pinned: boolean) {
    setBusy(true);
    try {
      await pinCatalogBackup(backupId, !pinned);
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "更新 Pin 失败");
    } finally {
      setBusy(false);
    }
  }

  async function scrub(backupId: string) {
    setBusy(true);
    try {
      const result = await scrubCatalogBackup(backupId);
      onMessage(result.ok ? `Scrub 通过：${backupId}` : `Scrub 失败：${Object.values(result.checks).filter((item) => item !== "PASS").join("; ")}`);
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Scrub 失败");
    } finally {
      setBusy(false);
    }
  }

  async function drill() {
    setBusy(true);
    try {
      const result = await verifyUnlockCatalogBackup(drillFor, identity);
      onMessage(`解锁演练通过：${result.backupId}（${result.userUnlockVerifiedAt}）`);
      setIdentity("");
      setDrillFor("");
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "解锁演练失败");
    } finally {
      setBusy(false);
    }
  }

  function healthFor(backupId: string) {
    return view?.health.backups.find((item) => item.backupId === backupId);
  }

  return (
    <section className="backup-card">
      <h3>备份库</h3>
      <p>
        Catalog 哈希链：{view?.chainValid ? "完整" : "损坏（可由 Receipt 重建）"}
        {view?.integrity.orphans.length ? ` · 孤儿文件 ${view.integrity.orphans.length}` : ""}
        {view?.integrity.missing.length ? ` · 丢失文件 ${view.integrity.missing.length}` : ""}
      </p>
      {!view?.backups.length && <p>还没有已编目的备份。</p>}
      {view?.backups.map((entry) => {
        const health = healthFor(entry.backupId);
        return (
          <div className="backup-policy" key={entry.backupId}>
            <div>
              <strong>{entry.filename}</strong>
              <dl>
                <div><dt>大小</dt><dd>{formatBytes(entry.size)}</dd></div>
                <div><dt>创建时间</dt><dd>{entry.createdAt}</dd></div>
                <div><dt>目标</dt><dd>{entry.targetId}</dd></div>
                <div><dt>创建验证</dt><dd>{entry.creationVerified ? "已完成无人值守往返" : "未验证"}</dd></div>
                <div><dt>Scrub</dt><dd>{entry.ciphertextScrubbedAt ? `${entry.scrubOk ? "通过" : "失败"} · ${entry.ciphertextScrubbedAt}` : "从未"}</dd></div>
                <div><dt>真实解锁验证</dt><dd>{entry.userUnlockVerifiedAt ?? "从未"}</dd></div>
                {health && health.issues.length > 0 && <div><dt>健康</dt><dd>{health.issues.join(", ")}</dd></div>}
              </dl>
            </div>
            <div className="backup-policy-actions">
              <label><input type="checkbox" checked={entry.pinned} disabled={busy} onChange={() => void togglePin(entry.backupId, entry.pinned)} /> Pin</label>
              <button type="button" disabled={busy} onClick={() => void scrub(entry.backupId)}>Scrub</button>
              <button type="button" disabled={busy} onClick={() => setDrillFor(entry.backupId)}>验证可恢复性</button>
            </div>
          </div>
        );
      })}
      {drillFor && (
        <div className="secret-fields">
          <p>对 {drillFor} 执行只读解锁演练。输入 Recovery Key；演练不会写入任何数据。</p>
          <textarea value={identity} aria-label="Recovery Key" autoComplete="off" spellCheck={false} onChange={(event) => setIdentity(event.target.value)} />
          <div>
            <button className="drawer-done" type="button" disabled={busy || !identity.startsWith("AGE-SECRET-KEY-")} onClick={() => void drill()}>解锁并验证</button>
            <button type="button" onClick={() => { setDrillFor(""); setIdentity(""); }}>取消</button>
          </div>
        </div>
      )}
    </section>
  );
}
