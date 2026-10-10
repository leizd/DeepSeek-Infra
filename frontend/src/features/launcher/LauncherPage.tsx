import { useEffect, useState, type FormEvent } from "react";
import { defaultLauncherConfig, LauncherError, launcherRequest, type LauncherConfig, type LauncherState } from "./api";
import "./launcher.css";

const statusText: Record<LauncherState["status"], string> = {
  stopped: "未启动", starting: "正在启动…", running: "服务运行中", stopping: "正在停止…", failed: "启动或运行失败",
};

function CredentialField({ label, value, onChange }: { label: string; value: string; onChange(value: string): void }) {
  const [show, setShow] = useState(false);
  const id = label.startsWith("DeepSeek") ? "launcher-deepseek-key" : "launcher-tavily-key";
  return <div className="launcher-field">
    <label htmlFor={id}>{label}</label>
    <div className="launcher-key-row">
      <input id={id} type={show ? "text" : "password"} value={value} maxLength={8192} autoComplete="off" spellCheck={false} onChange={(event) => onChange(event.target.value)} />
      <button type="button" aria-label={`${show ? "隐藏" : "显示"} ${label}`} aria-pressed={show} onClick={() => setShow(!show)}>{show ? "隐藏" : "显示"}</button>
    </div>
  </div>;
}

export function LauncherPage() {
  const [config, setConfig] = useState<LauncherConfig>(defaultLauncherConfig);
  const [port, setPort] = useState("8000");
  const [state, setState] = useState<LauncherState | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const active = state?.status === "starting" || state?.status === "running" || state?.status === "stopping";
  const update = (patch: Partial<LauncherConfig>) => setConfig((current) => ({ ...current, ...patch }));

  useEffect(() => {
    const abort = new AbortController();
    setLoaded(false);
    setError("");
    document.title = "DeepSeek Infra 启动器";
    void Promise.all([
      launcherRequest<LauncherConfig>("settings", undefined, abort.signal),
      launcherRequest<LauncherState>("state", undefined, abort.signal),
    ]).then(([saved, status]) => { setConfig(saved); setPort(String(saved.port)); setState(status); setLoaded(true); })
      .catch((cause: unknown) => { if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "无法连接启动器"); });
    const poll = window.setInterval(() => {
      void launcherRequest<LauncherState>("state", undefined, abort.signal)
        .then(setState).catch(() => { if (!abort.signal.aborted) setError("无法连接启动器，请重新打开启动器"); });
    }, 1000);
    return () => { abort.abort(); window.clearInterval(poll); };
  }, [reload]);

  function currentConfig(): LauncherConfig {
    const number = Number(port.trim() || "8000");
    if (!Number.isInteger(number) || number < 1 || number > 65535) throw new Error("请填写 1–65535 之间的端口号");
    return { ...config, port: number, deepseek_api_key: config.deepseek_api_key.trim(), tavily_api_key: config.tavily_api_key.trim() };
  }

  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); } catch (cause: unknown) { setError(cause instanceof Error ? cause.message : "启动器操作失败，请重试"); }
    finally { setBusy(false); }
  }

  function start(event: FormEvent) {
    event.preventDefault();
    void perform(async () => {
      const selected = currentConfig();
      if (!selected.deepseek_api_key && !window.confirm("尚未填写 DeepSeek API Key。可以稍后在网页设置中填写，是否继续启动？")) return;
      const request = { config: selected, confirm_missing_key: !selected.deepseek_api_key, confirm_next_port: false };
      let started: LauncherState;
      try { started = await launcherRequest<LauncherState>("start", request); }
      catch (cause) {
        if (!(cause instanceof LauncherError) || cause.code !== "PORT_IN_USE") throw cause;
        if (!window.confirm(`${cause.message}，将自动尝试下一个可用端口。是否继续启动？`)) return;
        started = await launcherRequest<LauncherState>("start", { ...request, confirm_next_port: true });
      }
      setState(started);
    });
  }

  const stop = () => void perform(async () => { setState(await launcherRequest<LauncherState>("stop", {})); });
  const save = () => void perform(async () => {
    const selected = currentConfig();
    await launcherRequest<LauncherState>("save", { config: selected });
    setConfig(selected); setPort(String(selected.port));
    setMessage(active ? "配置已加密保存，重新启动服务后应用。" : "配置已加密保存到本机。");
  });
  const clear = () => {
    if (!window.confirm("确认删除本机保存的 API Key 与配置？")) return;
    void perform(async () => { setState(await launcherRequest<LauncherState>("clear", { confirm: true })); setConfig(defaultLauncherConfig); setPort("8000"); setMessage("已清空本机保存的配置。"); });
  };
  const close = () => {
    if (active && !window.confirm("服务仍在运行。退出会停止本地服务，确定退出吗？")) return;
    void perform(async () => { await launcherRequest<LauncherState>("close", { confirm: Boolean(active) }); });
  };
  const copy = (value: string) => void perform(async () => { await navigator.clipboard.writeText(value); setMessage("地址已复制。"); });

  return <main className="launcher-page">
    <div className="launcher-content">
      <header><h1>DeepSeek Infra 启动器</h1><p>配置本地服务，打开网页后即可开始对话。</p></header>
      {!loaded && !error && <p role="status">正在读取本机配置…</p>}
      {error && <div className="launcher-notice launcher-error" role="alert">{error}{!loaded && <button type="button" onClick={() => setReload((value) => value + 1)}>重试</button>}</div>}
      {state?.error && <p className="launcher-notice launcher-error" role="alert">{state.error}</p>}
      {message && <p className="launcher-notice" role="status">{message}</p>}
      <form onSubmit={start} aria-busy={busy}>
        <fieldset disabled={!loaded || busy}>
          <legend>服务配置</legend>
          <CredentialField label="DeepSeek API Key" value={config.deepseek_api_key} onChange={(value) => update({ deepseek_api_key: value })} />
          <CredentialField label="Tavily API Key" value={config.tavily_api_key} onChange={(value) => update({ tavily_api_key: value })} />
          <div className="launcher-network">
            <label htmlFor="launcher-host">监听地址<input id="launcher-host" readOnly value={config.host} /></label>
            <label htmlFor="launcher-port">端口<input id="launcher-port" inputMode="numeric" value={port} onChange={(event) => setPort(event.target.value)} /></label>
          </div>
          <div className="launcher-options">
            <label><input type="checkbox" checked={config.allow_lan} onChange={(event) => update({ allow_lan: event.target.checked, host: event.target.checked ? "0.0.0.0" : "127.0.0.1" })} />允许局域网访问</label>
            <label><input type="checkbox" checked={config.ocr_enabled} onChange={(event) => update({ ocr_enabled: event.target.checked })} />启用 OCR</label>
            <label><input type="checkbox" checked={config.auth_disabled} onChange={(event) => update({ auth_disabled: event.target.checked })} />关闭网页 Token 认证</label>
          </div>
          <p className="launcher-hint">API Key 加密保存在本机；运行时修改配置会在下次启动生效。</p>
          <div className="launcher-actions">
            <button className="launcher-primary" type="submit" disabled={active}>启动服务</button>
            <button type="button" onClick={stop} disabled={!active || state?.status === "stopping"}>停止服务</button>
            <button type="button" onClick={() => void perform(async () => { await launcherRequest<LauncherState>("browser", {}); })} disabled={state?.status !== "running"}>打开网页</button>
            <button type="button" onClick={save}>保存配置</button>
            <button type="button" onClick={clear}>清空保存的 Key</button>
          </div>
        </fieldset>
      </form>
      <section className="launcher-status" aria-label="服务状态">
        <h2>服务状态</h2><p role="status" aria-live="polite">{state ? statusText[state.status] : "正在读取…"}</p>
        <div className="launcher-address"><span>本机地址</span><code>{state?.computer_url || "—"}</code><button type="button" disabled={!state?.computer_url || busy} onClick={() => state?.computer_url && copy(state.computer_url)}>复制本机地址</button></div>
        <div className="launcher-address"><span>手机地址</span><code>{state?.phone_urls.join("\n") || "—"}</code><button type="button" disabled={!state?.phone_urls.length || busy} onClick={() => state?.phone_urls.length && copy(state.phone_urls.join("\n"))}>复制手机地址</button></div>
        <p className="launcher-hint">手机需与电脑处于同一局域网，并启用“允许局域网访问”。</p>
      </section>
      <section className="launcher-log"><h2>运行日志</h2><pre aria-label="运行日志">{state?.logs.join("\n") || "服务尚未输出日志。"}</pre></section>
      <footer><button type="button" onClick={close} disabled={busy}>退出启动器</button></footer>
    </div>
  </main>;
}
