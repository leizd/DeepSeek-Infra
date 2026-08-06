import { useEffect, useState } from "react";

import {
  backupCapabilities,
  createBackupSession,
  finalizeBackup,
  generateRecoveryIdentity,
  inspectBackup,
  putBackupSecret,
  putRestoreSecret,
  unlockBackup,
  uploadFrontendBackupState,
  type BackupProtection,
  type BackupSession,
  type LockedRestoreUpload,
  type RestoreMode,
  type RestorePlan,
} from "../../api/workspaceBackupApi";
import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";
import { Icon } from "../../shared/ui/Icon";
import { applyCoordinatedWorkspaceRestore, collectFrontendBackupEnvelope } from "./frontendBackup";
import AutomaticBackupsTab from "./AutomaticBackupsTab";
import BackupLibraryTab from "./BackupLibraryTab";
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
  const [protectionMode, setProtectionMode] = useState<BackupProtection["mode"]>("passphrase");
  const [passphrase, setPassphrase] = useState("");
  const [passphraseConfirmation, setPassphraseConfirmation] = useState("");
  const [recoveryIdentity, setRecoveryIdentity] = useState("");
  const [recoveryRecipient, setRecoveryRecipient] = useState("");
  const [recoveryConfirmation, setRecoveryConfirmation] = useState("");
  const [includeExternalState, setIncludeExternalState] = useState(true);
  const [coveragePolicy, setCoveragePolicy] = useState<"strict" | "best-effort">("strict");
  const [externalStatus, setExternalStatus] = useState("未配置");
  const [locked, setLocked] = useState<LockedRestoreUpload | null>(null);
  const [unlockSecret, setUnlockSecret] = useState("");
  const [reattachSecret, setReattachSecret] = useState("");
  const [needsSecret, setNeedsSecret] = useState(false);
  const [activeTab, setActiveTab] = useState<"manual" | "automatic" | "library">("manual");

  const secretRequired = plan?.encrypted === true && (needsSecret || plan.secretState === "expired" || plan.secretState === "required-for-safety-backup");

  useEffect(() => {
    if (overlay.activeOverlay !== "backup-restore") return;
    void backupCapabilities()
      .then((value) => {
        const external = value.externalContributors.find((item) => item.id === "stateless-mcp");
        setExternalStatus(external?.available ? "可用" : external?.reason ?? "未连接");
        if (!value.encryptedBackupAvailable) setProtectionMode("none");
      })
      .catch(() => setExternalStatus("状态不可用"));
  }, [overlay.activeOverlay]);

  if (overlay.activeOverlay !== "backup-restore") return null;

  function clearSecrets() {
    setPassphrase("");
    setPassphraseConfirmation("");
    setRecoveryIdentity("");
    setRecoveryConfirmation("");
    setUnlockSecret("");
  }

  function close() {
    clearSecrets();
    overlay.closeOverlay();
  }

  async function createRecoveryKey() {
    setBusy(true);
    setError("");
    try {
      const generated = await generateRecoveryIdentity();
      setRecoveryIdentity(generated.identity);
      setRecoveryRecipient(generated.recipient);
      setRecoveryConfirmation("");
      setMessage("Recovery Key 只显示一次。请离线保存，并在下方重新粘贴确认。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生成 Recovery Key 失败");
    } finally {
      setBusy(false);
    }
  }

  async function createBackup() {
    setBusy(true);
    setError("");
    setMessage("正在收敛浏览器会话…");
    try {
      const flush = chat.flushConversationPersistence();
      if (!flush.ok) throw new Error(flush.message);
      const capabilities = await backupCapabilities();
      if (capabilities.purpose !== "restorable-backup") throw new Error("服务端不支持可恢复备份");
      if (protectionMode !== "none" && !capabilities.encryptedBackupAvailable) throw new Error(capabilities.reason ?? "加密备份不可用");
      if (protectionMode === "passphrase" && (passphrase.length < 8 || passphrase !== passphraseConfirmation)) {
        throw new Error("密码至少 8 个字符，且两次输入必须一致");
      }
      if (protectionMode === "age-recipient" && (!recoveryRecipient || recoveryConfirmation !== recoveryIdentity)) {
        throw new Error("请先生成、保存并重新导入 Recovery Key 完成确认");
      }
      const protection: BackupProtection = protectionMode === "age-recipient"
        ? { mode: "age-recipient", recipients: [recoveryRecipient] }
        : { mode: protectionMode };
      const envelope = await collectFrontendBackupEnvelope(settings.runtime?.version ?? "4.4.2", includeDrafts);
      const created = await createBackupSession({
        mode: "full",
        projectIds: [],
        includeHistory,
        includeDrafts,
        includeRebuildableIndexes: false,
        includeExternalState,
        coveragePolicy,
        protection,
      });
      setMessage("正在生成并验证备份包…");
      await uploadFrontendBackupState(created.backupId, envelope);
      if (protectionMode === "passphrase") {
        await putBackupSecret(created.backupId, { kind: "passphrase", secret: passphrase });
      } else if (protectionMode === "age-recipient") {
        await putBackupSecret(created.backupId, { kind: "age-identity", secret: recoveryConfirmation });
      }
      const ready = await finalizeBackup(created.backupId);
      setBackup(ready);
      setMessage(protectionMode === "none"
        ? "明文备份已逐文件校验，请按完整工作区敏感数据保管。"
        : "加密备份已完成 age 认证往返验证，可安全下载。");
      clearSecrets();
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
    setLocked(null);
    try {
      const inspected = await inspectBackup(file);
      if (inspected.phase === "locked") {
        setLocked(inspected);
        setMessage("检测到加密备份。提供密码或 Recovery Key 后才会解析工作区元数据。");
        return;
      }
      if (inspected.purpose !== "restorable-backup") throw new Error("分享 Export 不能用于恢复");
      setPlan(inspected);
      setMessage("检查完成。应用前会自动创建安全快照。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "恢复包检查失败");
    } finally {
      setBusy(false);
    }
  }

  async function unlock() {
    if (!locked || !unlockSecret) return;
    setBusy(true);
    setError("");
    try {
      await putRestoreSecret(locked.restoreId, {
        kind: locked.protection === "passphrase" ? "passphrase" : "age-identity",
        secret: unlockSecret,
      });
      const inspected = await unlockBackup(locked.restoreId);
      setPlan(inspected);
      setLocked(null);
      setUnlockSecret("");
      setMessage("加密包认证完成；恢复计划已验证。应用前会创建同等保护的安全快照。");
    } catch (reason) {
      setUnlockSecret("");
      setError(reason instanceof Error ? reason.message : "无法解锁备份");
    } finally {
      setBusy(false);
    }
  }

  async function reattach() {
    if (!plan || !reattachSecret) return;
    setBusy(true);
    setError("");
    try {
      await putRestoreSecret(plan.restoreId, {
        kind: plan.protection === "passphrase" ? "passphrase" : "age-identity",
        secret: reattachSecret,
      });
      const refreshed = await unlockBackup(plan.restoreId);
      setPlan(refreshed);
      setNeedsSecret(false);
      setReattachSecret("");
      setMessage("秘密已重新挂接；已确认的恢复计划保持不变。");
    } catch (reason) {
      setReattachSecret("");
      setError(reason instanceof Error ? reason.message : "无法重新挂接秘密");
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
      const text = reason instanceof Error ? reason.message : "应用恢复失败";
      if (plan.encrypted && /required or expired|re-provide|重新提供/.test(text)) {
        setNeedsSecret(true);
        setError("秘密已过期或服务已重启；请重新提供以继续恢复，无需重新上传备份包。");
      } else {
        setError(text);
      }
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
        <button type="button" aria-label="关闭备份与恢复" onClick={close}><Icon name="close" /></button>
      </div>

      <div className="backup-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={activeTab === "manual"} className={activeTab === "manual" ? "active" : ""} onClick={() => setActiveTab("manual")}>立即备份</button>
        <button type="button" role="tab" aria-selected={activeTab === "automatic"} className={activeTab === "automatic" ? "active" : ""} onClick={() => setActiveTab("automatic")}>自动备份</button>
        <button type="button" role="tab" aria-selected={activeTab === "library"} className={activeTab === "library" ? "active" : ""} onClick={() => setActiveTab("library")}>备份库</button>
      </div>

      {activeTab === "automatic" && <AutomaticBackupsTab onError={setError} onMessage={setMessage} />}
      {activeTab === "library" && <BackupLibraryTab onError={setError} onMessage={setMessage} formatBytes={formatBytes} />}
      {activeTab === "manual" && (
        <>
      <section className="backup-card">
        <h3>创建完整工作区备份</h3>
        <p>包含项目、源文件、记忆、媒体、自定义技能、自动化和已校验的浏览器会话。凭据、锁、缓存索引和活动任务永不纳入。</p>
        <label><input type="checkbox" checked={includeHistory} onChange={(event) => setIncludeHistory(event.target.checked)} /> 包含可选运行历史</label>
        <label><input type="checkbox" checked={includeDrafts} onChange={(event) => setIncludeDrafts(event.target.checked)} /> 包含 Composer 草稿</label>
        <fieldset className="backup-options">
          <legend>备份保护</legend>
          <label><input type="radio" name="backup-protection" checked={protectionMode === "none"} onChange={() => setProtectionMode("none")} /> 不加密</label>
          <label><input type="radio" name="backup-protection" checked={protectionMode === "passphrase"} onChange={() => setProtectionMode("passphrase")} /> 使用密码</label>
          <label><input type="radio" name="backup-protection" checked={protectionMode === "age-recipient"} onChange={() => setProtectionMode("age-recipient")} /> 使用 Recovery Key</label>
          {protectionMode === "passphrase" && (
            <div className="secret-fields">
              <input type="password" value={passphrase} placeholder="密码" autoComplete="new-password" spellCheck={false} onChange={(event) => setPassphrase(event.target.value)} />
              <input type="password" value={passphraseConfirmation} placeholder="确认密码" autoComplete="new-password" spellCheck={false} onChange={(event) => setPassphraseConfirmation(event.target.value)} />
              <small>仅本次使用，不会保存。</small>
            </div>
          )}
          {protectionMode === "age-recipient" && (
            <div className="secret-fields">
              <button type="button" disabled={busy} onClick={() => void createRecoveryKey()}>生成 Recovery Key</button>
              {recoveryIdentity && <textarea readOnly aria-label="一次性 Recovery Key" value={recoveryIdentity} spellCheck={false} />}
              <textarea value={recoveryConfirmation} aria-label="重新导入 Recovery Key" placeholder="重新粘贴 Recovery Key 以确认" autoComplete="off" spellCheck={false} onChange={(event) => setRecoveryConfirmation(event.target.value)} />
            </div>
          )}
        </fieldset>
        <fieldset className="backup-options">
          <legend>数据覆盖</legend>
          <p>本地工作区：已包含 · 浏览器会话：已包含</p>
          <label><input type="checkbox" checked={includeExternalState} onChange={(event) => setIncludeExternalState(event.target.checked)} /> Stateless MCP：{externalStatus}</label>
          <label>覆盖策略 <select value={coveragePolicy} onChange={(event) => setCoveragePolicy(event.target.value as "strict" | "best-effort")}><option value="strict">严格：外部持久状态缺失则失败</option><option value="best-effort">尽力：明确记录遗漏</option></select></label>
          <p>重建索引：已排除 · 凭据：永不包含</p>
        </fieldset>
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
          accept=".dsibackup,.age,.dsibackup.age,application/vnd.deepseek-infra.backup+zip,application/age"
          disabled={busy}
          onChange={(event) => void inspect(event.target.files?.[0])}
        />
        {locked && (
          <div className="restore-plan secret-fields">
            <p>{locked.protection === "passphrase" ? "输入备份密码" : "选择并粘贴 Recovery Key"}</p>
            <textarea
              value={unlockSecret}
              aria-label="解锁备份"
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setUnlockSecret(event.target.value)}
            />
            <button className="drawer-done" type="button" disabled={busy || !unlockSecret} onClick={() => void unlock()}>解锁并检查</button>
          </div>
        )}
        {plan && (
          <div className="restore-plan">
            <dl>
              <div><dt>来源版本</dt><dd>{plan.sourceVersion}</dd></div>
              <div><dt>目标版本</dt><dd>{plan.targetVersion}</dd></div>
              <div><dt>预计写入</dt><dd>{formatBytes(plan.estimatedWriteBytes)}</dd></div>
              <div><dt>ID / 文件冲突</dt><dd>{plan.conflicts.reduce((total, item) => total + item.count, 0)}</dd></div>
            </dl>
            {secretRequired ? (
              <div className="secret-fields">
                <p>{plan.protection === "passphrase" ? "秘密已过期，重新输入备份密码" : "秘密已过期，重新粘贴 Recovery Key"}（无需重新上传备份包）</p>
                <textarea
                  value={reattachSecret}
                  aria-label="重新提供备份秘密"
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) => setReattachSecret(event.target.value)}
                />
                <button className="drawer-done" type="button" disabled={busy || !reattachSecret} onClick={() => void reattach()}>重新挂接秘密</button>
              </div>
            ) : (
              <>
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
              </>
            )}
          </div>
        )}
      </section>
        </>
      )}
      {message && <p className="backup-status" role="status">{message}</p>}
      {error && <p className="message-error" role="alert">{error}</p>}
    </section>
  );
}
