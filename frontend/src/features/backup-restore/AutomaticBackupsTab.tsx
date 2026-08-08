import { useCallback, useEffect, useState } from "react";

import {
  createBackupPolicy,
  createBackupTarget,
  deleteBackupPolicy,
  listBackupMirrors,
  listBackupPolicies,
  listBackupTargets,
  probeBackupTarget,
  runBackupPolicy,
  updateBackupPolicy,
  type BackupMirrorMetadataV1,
  type BackupNextRun,
  type BackupPolicyV1,
  type BackupTargetHealth,
  type BackupTargetRecord,
} from "../../api/workspaceBackupApi";

interface Props {
  onError: (message: string) => void;
  onMessage: (message: string) => void;
}

export default function AutomaticBackupsTab({ onError, onMessage }: Props) {
  const [policies, setPolicies] = useState<BackupPolicyV1[]>([]);
  const [nextRuns, setNextRuns] = useState<Record<string, BackupNextRun | null>>({});
  const [targets, setTargets] = useState<BackupTargetRecord[]>([]);
  const [health, setHealth] = useState<BackupTargetHealth[]>([]);
  const [mirrors, setMirrors] = useState<BackupMirrorMetadataV1[]>([]);
  const [busy, setBusy] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [cron, setCron] = useState("0 3 * * *");
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [recipient, setRecipient] = useState("");
  const [targetId, setTargetId] = useState("managed-local");
  const [mirrorMode, setMirrorMode] = useState<"required" | "best-effort" | "excluded">("best-effort");
  const [coveragePolicy, setCoveragePolicy] = useState<"strict" | "best-effort">("strict");
  const [targetKind, setTargetKind] = useState<"filesystem" | "s3">("filesystem");
  const [targetPath, setTargetPath] = useState("");
  const [targetLabel, setTargetLabel] = useState("");
  const [s3Bucket, setS3Bucket] = useState("");
  const [s3Prefix, setS3Prefix] = useState("");
  const [s3Region, setS3Region] = useState("");
  const [s3Endpoint, setS3Endpoint] = useState("");
  const [s3Profile, setS3Profile] = useState("");

  const refresh = useCallback(async () => {
    const [policyView, targetView, mirrorView] = await Promise.all([listBackupPolicies(), listBackupTargets(), listBackupMirrors()]);
    setPolicies(policyView.policies);
    setNextRuns(policyView.nextRuns);
    setTargets(targetView.targets);
    setHealth(targetView.health);
    setMirrors(mirrorView.mirrors);
  }, []);

  useEffect(() => {
    void refresh().catch((reason: unknown) => onError(reason instanceof Error ? reason.message : "加载自动备份配置失败"));
  }, [refresh, onError]);

  async function toggle(policy: BackupPolicyV1) {
    setBusy(true);
    try {
      await updateBackupPolicy(policy.policyId, { enabled: !policy.enabled });
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "更新策略失败");
    } finally {
      setBusy(false);
    }
  }

  async function remove(policy: BackupPolicyV1) {
    if (!window.confirm(`删除定时备份策略「${policy.name}」？已创建的备份不会被删除。`)) return;
    setBusy(true);
    try {
      await deleteBackupPolicy(policy.policyId);
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "删除策略失败");
    } finally {
      setBusy(false);
    }
  }

  async function runNow(policy: BackupPolicyV1) {
    setBusy(true);
    try {
      const outcome = await runBackupPolicy(policy.policyId);
      if (outcome.phase === "complete") {
        onMessage(`备份完成：${outcome.filename}`);
      } else {
        onError(`运行结束于 ${outcome.phase}：${outcome.error ?? outcome.reason ?? ""}`);
      }
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "立即运行失败");
    } finally {
      setBusy(false);
    }
  }

  async function createPolicy() {
    setBusy(true);
    onError("");
    try {
      if (!name.trim()) throw new Error("请填写策略名称");
      if (!recipient.trim().startsWith("age1")) throw new Error("请填写公开的 age1... Recovery Recipient");
      await createBackupPolicy({
        schemaVersion: 1,
        name: name.trim(),
        enabled: true,
        schedule: { cron: cron.trim(), timezone: timezone.trim(), misfirePolicy: "skip", catchupWindowSeconds: 86400, jitterSeconds: 0 },
        scope: { mode: "full", projectIds: [], includeHistory: true, includeExternalState: true, coveragePolicy },
        frontendMirror: { mode: mirrorMode, maxAgeSeconds: 3600 },
        protection: { mode: "age-recipient", recipients: [recipient.trim()] },
        targetId,
        retentionPolicyId: "default",
      });
      setShowForm(false);
      setName("");
      setRecipient("");
      onMessage("定时备份策略已创建");
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "创建策略失败");
    } finally {
      setBusy(false);
    }
  }

  async function registerTarget() {
    setBusy(true);
    try {
      if (targetKind === "s3") {
        if (!s3Bucket.trim()) throw new Error("请填写 S3 Bucket");
        await createBackupTarget({
          kind: "s3",
          bucket: s3Bucket.trim(),
          prefix: s3Prefix.trim(),
          region: s3Region.trim() || undefined,
          endpointUrl: s3Endpoint.trim() || undefined,
          label: targetLabel.trim(),
          credentialProvider: s3Profile.trim()
            ? { type: "aws-profile", profile: s3Profile.trim() }
            : { type: "aws-default-chain" },
        });
        setS3Bucket("");
        setS3Prefix("");
        setS3Region("");
        setS3Endpoint("");
        setS3Profile("");
      } else {
        await createBackupTarget({ path: targetPath.trim(), label: targetLabel.trim() });
        setTargetPath("");
      }
      setTargetLabel("");
      onMessage("备份目标已注册");
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "注册目标失败");
    } finally {
      setBusy(false);
    }
  }

  async function probe(target: BackupTargetRecord) {
    setBusy(true);
    try {
      const result = await probeBackupTarget(target.targetId);
      if (result.ready) {
        const ready = result.scheduledBackupReady === false ? "已连接但条件写未就绪" : "可用（定时备份就绪）";
        onMessage(`目标 ${target.label || target.targetId} ${ready}`);
      } else {
        onError(`目标不可用：${result.detail ?? result.status}`);
      }
      await refresh();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "探测目标失败");
    } finally {
      setBusy(false);
    }
  }

  function targetSummary(target: BackupTargetRecord): string {
    if ((target.kind || "filesystem") === "s3") {
      const prefix = target.prefix ? `/${target.prefix}` : "";
      return `s3://${target.bucket || "?"}${prefix}`;
    }
    return target.path || "—";
  }

  function healthFor(id: string): string {
    return health.find((item) => item.targetId === id)?.status ?? "未知";
  }

  return (
    <>
      <section className="backup-card">
        <h3>定时备份策略</h3>
        <p className="backup-warning">
          自动备份只保存公开 Recipient，不保存 Recovery Key。丢失 Recovery Key 后，系统无法帮助解锁备份。
        </p>
        {policies.length === 0 && <p>还没有定时备份策略。</p>}
        {policies.map((policy) => (
          <div className="backup-policy" key={policy.policyId}>
            <div>
              <strong>{policy.name}</strong>
              <dl>
                <div><dt>计划</dt><dd>{policy.schedule.cron}（{policy.schedule.timezone}）</dd></div>
                <div><dt>下一次运行</dt><dd>{nextRuns[policy.policyId]?.localDateTime ?? "—"}</dd></div>
                <div><dt>目标</dt><dd>{policy.targetId === "managed-local" ? "本地托管" : policy.targetId}（{healthFor(policy.targetId)}）</dd></div>
                <div><dt>Recipient</dt><dd>{policy.protection.recipients.length} 个公开 Recipient</dd></div>
                <div><dt>会话镜像</dt><dd>{policy.frontendMirror.mode} · {mirrors.length ? `最新镜像 ${mirrors[0].acknowledgedAt}` : "尚未上传"}</dd></div>
                <div><dt>覆盖</dt><dd>{policy.scope.coveragePolicy === "strict" ? "严格" : "尽力"}</dd></div>
              </dl>
            </div>
            <div className="backup-policy-actions">
              <label><input type="checkbox" checked={policy.enabled} disabled={busy} onChange={() => void toggle(policy)} /> 启用</label>
              <button type="button" disabled={busy} onClick={() => void runNow(policy)}>立即运行</button>
              <button type="button" disabled={busy} onClick={() => void remove(policy)}>删除</button>
            </div>
          </div>
        ))}
        {!showForm ? (
          <button className="drawer-done" type="button" onClick={() => setShowForm(true)}>新建定时备份策略</button>
        ) : (
          <div className="secret-fields">
            <input value={name} placeholder="策略名称" onChange={(event) => setName(event.target.value)} />
            <input value={cron} aria-label="Cron 表达式" placeholder="0 3 * * *" onChange={(event) => setCron(event.target.value)} />
            <input value={timezone} aria-label="IANA 时区" placeholder="Asia/Singapore" onChange={(event) => setTimezone(event.target.value)} />
            <textarea value={recipient} aria-label="公开 Recovery Recipient" placeholder="age1..." spellCheck={false} onChange={(event) => setRecipient(event.target.value)} />
            <label>目标
              <select value={targetId} onChange={(event) => setTargetId(event.target.value)}>
                <option value="managed-local">本地托管</option>
                {targets.map((target) => (
                  <option key={target.targetId} value={target.targetId}>{target.label || target.targetId}</option>
                ))}
              </select>
            </label>
            <label>浏览器会话
              <select value={mirrorMode} onChange={(event) => setMirrorMode(event.target.value as typeof mirrorMode)}>
                <option value="required">必须包含密封镜像</option>
                <option value="best-effort">尽力包含（记录缺失）</option>
                <option value="excluded">不包含</option>
              </select>
            </label>
            <label>覆盖策略
              <select value={coveragePolicy} onChange={(event) => setCoveragePolicy(event.target.value as typeof coveragePolicy)}>
                <option value="strict">严格</option>
                <option value="best-effort">尽力</option>
              </select>
            </label>
            <button className="drawer-done" type="button" disabled={busy} onClick={() => void createPolicy()}>创建策略</button>
          </div>
        )}
      </section>

      <section className="backup-card">
        <h3>备份目标</h3>
        <p>
          本地目录以 Marker 识别；S3-compatible 目标使用条件写（If-None-Match / If-Match）与 multipart。
          Access Key 不会写入 Target 配置——请使用 AWS profile、默认凭证链或 workload role。
        </p>
        {targets.map((target) => (
          <div className="backup-policy" key={target.targetId}>
            <div>
              <strong>{target.label || target.targetId}</strong>
              <dl>
                <div><dt>类型</dt><dd>{target.kind || "filesystem"}</dd></div>
                <div><dt>位置</dt><dd>{targetSummary(target)}</dd></div>
                <div><dt>健康</dt><dd>{healthFor(target.targetId)}</dd></div>
                {target.lastProbe?.scheduledBackupReady != null && (
                  <div><dt>定时备份</dt><dd>{target.lastProbe.scheduledBackupReady ? "READY" : "unsupported-conditional-target"}</dd></div>
                )}
              </dl>
            </div>
            <div className="backup-policy-actions">
              <button type="button" disabled={busy} onClick={() => void probe(target)}>探测</button>
            </div>
          </div>
        ))}
        <div className="secret-fields">
          <label>目标类型
            <select value={targetKind} onChange={(event) => setTargetKind(event.target.value as typeof targetKind)}>
              <option value="filesystem">本地目录</option>
              <option value="s3">S3-compatible</option>
            </select>
          </label>
          {targetKind === "filesystem" ? (
            <input value={targetPath} placeholder="目标目录绝对路径（如 D:\\backups 或 /mnt/backup）" onChange={(event) => setTargetPath(event.target.value)} />
          ) : (
            <>
              <input value={s3Bucket} placeholder="Bucket" onChange={(event) => setS3Bucket(event.target.value)} />
              <input value={s3Prefix} placeholder="Prefix（可选，如 deepseek-infra/home）" onChange={(event) => setS3Prefix(event.target.value)} />
              <input value={s3Region} placeholder="Region（可选）" onChange={(event) => setS3Region(event.target.value)} />
              <input value={s3Endpoint} placeholder="Endpoint URL（S3 兼容可选）" onChange={(event) => setS3Endpoint(event.target.value)} />
              <input value={s3Profile} placeholder="AWS profile（可选；默认凭证链）" onChange={(event) => setS3Profile(event.target.value)} autoComplete="off" />
            </>
          )}
          <input value={targetLabel} placeholder="标签（可选）" onChange={(event) => setTargetLabel(event.target.value)} />
          <button
            type="button"
            disabled={busy || (targetKind === "filesystem" ? !targetPath.trim() : !s3Bucket.trim())}
            onClick={() => void registerTarget()}
          >
            注册目标
          </button>
        </div>
      </section>
    </>
  );
}
