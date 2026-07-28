import { useState } from "react";

import {
  backupCapabilities,
  createBackupSession,
  finalizeBackup,
  inspectBackup,
  uploadFrontendBackupState,
  type BackupSession,
  type RestoreMode,
  type RestorePlan,
} from "../../api/workspaceBackupApi";
import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";
import { Icon } from "../../shared/ui/Icon";
import { applyCoordinatedWorkspaceRestore, collectFrontendBackupEnvelope } from "./frontendBackup";
import "../workspace/workspace-optional.css";
import "./backup-restore.css";

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

export default function BackupRestoreFeature() {
  const overlay = useOverlay();
  const chat = useChat();
  const settings = useSettings();
  const [includeHistory, setIncludeHistory] = useState(false);
  const [includeDrafts, setIncludeDrafts] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [backup, setBackup] = useState<BackupSession | null>(null);
  const [plan, setPlan] = useState<RestorePlan | null>(null);
  const [restoreMode, setRestoreMode] = useState<RestoreMode>("merge");

  if (overlay.activeOverlay !== "backup-restore") return null;

  async function createBackup() {
    setBusy(true);
    setError("");
    setMessage("正在收敛浏览器会话…");
    try {
      const flush = chat.flushConversationPersistence();
      if (!flush.ok) throw new Error(flush.message);
      const capabilities = await backupCapabilities();
      if (capabilities.purpose !== "restorable-backup") throw new Error("服务端不支持可恢复备份");
      const envelope = await collectFrontendBackupEnvelope(settings.runtime?.version ?? "4.4.1", includeDrafts);
      const created = await createBackupSession({
        mode: "full",
        projectIds: [],
        includeHistory,
        includeDrafts,
        includeRebuildableIndexes: false,
      });
      setMessage("正在生成并验证备份包…");
      await uploadFrontendBackupState(created.backupId, envelope);
      const ready = await finalizeBackup(created.backupId);
      setBackup(ready);
      setMessage("备份已逐文件校验，可安全下载。此文件默认未加密，请妥善保管。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建备份失败");
    } finally {
      setBusy(false);
    }
  }

  async function inspect(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    setError("");
    setMessage("只读检查恢复包；当前工作区不会被修改…");
    setPlan(null);
    try {
      const inspected = await inspectBackup(file);
      if (inspected.purpose !== "restorable-backup") throw new Error("分享 Export 不能用于恢复");
      setPlan(inspected);
      setMessage("检查完成。应用前会自动创建安全快照。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "恢复包检查失败");
    } finally {
      setBusy(false);
    }
  }

  async function applyRestore() {
    if (!plan) return;
    setBusy(true);
    setError("");
    setMessage("正在创建安全快照并事务化恢复…");
    try {
      const flush = chat.flushConversationPersistence();
      if (!flush.ok) throw new Error(`当前标签页尚未安全落盘：${flush.message}`);
      await applyCoordinatedWorkspaceRestore(plan.restoreId, restoreMode);
      setMessage("恢复已提交。即将进行一次受控刷新以载入新工作区。");
      window.setTimeout(() => window.location.reload(), 500);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "应用恢复失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="settings-drawer workspace-drawer backup-restore-drawer" role="dialog" aria-modal="true" aria-labelledby="backup-restore-title">
      <div className="drawer-heading">
        <div>
          <p className="eyebrow">PORTABLE DATA</p>
          <h2 id="backup-restore-title">备份与恢复</h2>
        </div>
        <button type="button" aria-label="关闭备份与恢复" onClick={overlay.closeOverlay}><Icon name="close" /></button>
      </div>

      <section className="backup-card">
        <h3>创建完整工作区备份</h3>
        <p>包含项目、源文件、记忆、媒体、自定义技能、自动化和已校验的浏览器会话。凭据、锁、缓存索引和活动任务永不纳入。</p>
        <label><input type="checkbox" checked={includeHistory} onChange={(event) => setIncludeHistory(event.target.checked)} /> 包含可选运行历史</label>
        <label><input type="checkbox" checked={includeDrafts} onChange={(event) => setIncludeDrafts(event.target.checked)} /> 包含 Composer 草稿</label>
        <button className="drawer-done" type="button" disabled={busy} onClick={() => void createBackup()}>
          {busy ? "处理中…" : "创建并验证"}
        </button>
        {backup?.downloadUrl && (
          <a className="backup-download" href={backup.downloadUrl} download={backup.filename}>下载 {backup.filename ?? ".dsibackup"}</a>
        )}
      </section>

      <section className="backup-card">
        <h3>检查恢复包</h3>
        <p>先验证格式、路径、SHA-256、Schema、空间和冲突；检查阶段不会写入正式数据。</p>
        <input
          aria-label="选择 dsibackup 恢复包"
          type="file"
          accept=".dsibackup,application/vnd.deepseek-infra.backup+zip"
          disabled={busy}
          onChange={(event) => void inspect(event.target.files?.[0])}
        />
        {plan && (
          <div className="restore-plan">
            <dl>
              <div><dt>来源版本</dt><dd>{plan.sourceVersion}</dd></div>
              <div><dt>目标版本</dt><dd>{plan.targetVersion}</dd></div>
              <div><dt>预计写入</dt><dd>{formatBytes(plan.estimatedWriteBytes)}</dd></div>
              <div><dt>ID / 文件冲突</dt><dd>{plan.conflicts.reduce((total, item) => total + item.count, 0)}</dd></div>
            </dl>
            <label>
              恢复方式
              <select value={restoreMode} onChange={(event) => setRestoreMode(event.target.value as RestoreMode)}>
                <option value="merge">合并（冲突生成确定性副本）</option>
                <option value="project-copy">作为项目副本</option>
                <option value="replace-empty">仅恢复到空工作区</option>
              </select>
            </label>
            <button className="drawer-done danger-confirm" type="button" disabled={busy || !plan.compatible} onClick={() => void applyRestore()}>
              创建安全快照并恢复
            </button>
          </div>
        )}
      </section>
      {message && <p className="backup-status" role="status">{message}</p>}
      {error && <p className="message-error" role="alert">{error}</p>}
    </section>
  );
}
