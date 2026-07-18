import { useState } from "react";

import { useOverlay } from "../../contexts/OverlayContext";
import { useSettings } from "../../contexts/SettingsContext";

export function ConnectionSettingsDrawer() {
  const overlay = useOverlay();
  const settings = useSettings();
  const [showKeys, setShowKeys] = useState(false);
  if (overlay.activeOverlay !== "settings") return null;
  const hasGeneration = Boolean(settings.apiKey.trim() || settings.runtime?.hasServerKey);
  const hasSearch = Boolean(settings.tavilyApiKey.trim() || settings.runtime?.hasSearch);

  return (
    <section className="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="connection-settings-title">
      <div className="drawer-heading">
        <div>
          <p className="eyebrow">SESSION CREDENTIALS</p>
          <h2 id="connection-settings-title">连接设置</h2>
        </div>
        <button type="button" aria-label="关闭连接设置" onClick={overlay.closeOverlay}>×</button>
      </div>
      <p className="credential-note">页面内输入的密钥只保存在当前 React 内存中；刷新或关闭后会清空，不写入 localStorage。</p>
      <label className="credential-field">
        <span>DeepSeek API Key</span>
        <input
          id="reactApiKeyInput"
          type={showKeys ? "text" : "password"}
          autoComplete="off"
          spellCheck={false}
          value={settings.apiKey}
          placeholder={settings.runtime?.hasServerKey ? "服务端已配置，可留空" : "输入本次会话使用的 Key"}
          onChange={(event) => settings.setApiKey(event.target.value)}
        />
      </label>
      <label className="credential-field">
        <span>Tavily API Key（联网搜索可选）</span>
        <input
          id="reactTavilyKeyInput"
          type={showKeys ? "text" : "password"}
          autoComplete="off"
          spellCheck={false}
          value={settings.tavilyApiKey}
          placeholder={settings.runtime?.hasSearch ? "服务端已配置，可留空" : "仅开启联网时使用"}
          onChange={(event) => settings.setTavilyApiKey(event.target.value)}
        />
      </label>
      <label className="show-key-toggle">
        <input type="checkbox" checked={showKeys} onChange={(event) => setShowKeys(event.target.checked)} />
        临时显示密钥
      </label>
      <label className="credential-field">
        <span>多 Agent 执行方式</span>
        <select
          id="reactAgentPresetSelect"
          value={settings.agentPreset}
          onChange={(event) => settings.setAgentPreset(event.target.value)}
        >
          <option value="full">完整直跑（4-Agent）</option>
          <option value="auto">自动选择</option>
          <option value="plan">先确认计划</option>
        </select>
      </label>
      <div className="connection-status-grid">
        <div><span className={hasGeneration ? "status-ok" : "status-warn"} />普通对话：{hasGeneration ? "可用" : "待配置"}</div>
        <div><span className={hasSearch ? "status-ok" : "status-warn"} />联网搜索：{hasSearch ? "可用" : "未配置"}</div>
      </div>
      {settings.error && <p className="message-error" role="alert">服务端配置读取失败：{settings.error}</p>}
      <button className="drawer-done" type="button" onClick={overlay.closeOverlay}>完成</button>
    </section>
  );
}
