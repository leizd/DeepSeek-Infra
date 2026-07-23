import { Component, type ErrorInfo, type ReactNode } from "react";

import type { WorkspaceFeature } from "./workspaceFeatureRegistry";

const FEATURE_LABELS: Record<WorkspaceFeature, string> = {
  settings: "连接设置",
  projects: "项目面板",
  skills: "技能面板",
  memory: "记忆面板",
  reminders: "提醒面板",
  diagnostics: "诊断面板",
  "file-preview": "文件预览",
  "image-lightbox": "图片预览",
  activity: "活动面板",
};

interface WorkspaceFeatureBoundaryProps {
  children: ReactNode;
  feature: WorkspaceFeature;
  onClose(): void;
  onRetry(): void;
}

interface WorkspaceFeatureBoundaryState {
  error: Error | null;
}

export class WorkspaceFeatureBoundary extends Component<
  WorkspaceFeatureBoundaryProps,
  WorkspaceFeatureBoundaryState
> {
  state: WorkspaceFeatureBoundaryState = { error: null };

  static getDerivedStateFromError(reason: unknown): WorkspaceFeatureBoundaryState {
    return { error: reason instanceof Error ? reason : new Error("Workspace feature failed to load") };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // The local recovery UI contains chunk failures so the chat shell remains usable.
  }

  render() {
    if (!this.state.error) return this.props.children;
    const chunkFailure = /dynamically imported|loading chunk|importing a module|fetch/i.test(this.state.error.message);
    return (
      <section className="settings-drawer workspace-drawer workspace-feature-error" role="alertdialog" aria-modal="true">
        <div className="drawer-heading">
          <div>
            <p className="eyebrow">WORKSPACE</p>
            <h2>{FEATURE_LABELS[this.props.feature]}加载失败</h2>
          </div>
        </div>
        <p className="workspace-feature-error-message">
          {chunkFailure ? "可重试加载；如果应用刚刚更新，请刷新页面后再试。" : this.state.error.message}
        </p>
        <div className="workspace-feature-error-actions">
          <button type="button" onClick={this.props.onRetry}>重试</button>
          <button type="button" onClick={this.props.onClose}>关闭</button>
        </div>
      </section>
    );
  }
}
