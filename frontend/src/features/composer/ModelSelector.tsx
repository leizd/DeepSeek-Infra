import { useSettings } from "../../contexts/SettingsContext";

function modelLabel(model: string): string {
  if (model.includes("flash")) return "DeepSeek V4 Flash";
  if (model.includes("pro")) return "DeepSeek V4 Pro";
  return model;
}

export function ModelSelector() {
  const settings = useSettings();
  const models = settings.runtime?.models ?? ["deepseek-v4-flash", "deepseek-v4-pro"];
  return (
    <label className="model-selector">
      <span className="sr-only">选择模型</span>
      <select value={settings.model} onChange={(event) => settings.setModel(event.target.value)} disabled={settings.loading}>
        {models.map((model) => <option value={model} key={model}>{modelLabel(model)}</option>)}
      </select>
    </label>
  );
}
