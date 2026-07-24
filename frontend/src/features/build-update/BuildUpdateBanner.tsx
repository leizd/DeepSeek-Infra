import { useEffect, useSyncExternalStore, type CSSProperties } from "react";
import { useIsMutating } from "@tanstack/react-query";

import { buildUpdateStore } from "../../app/buildUpdateStore";
import {
  clearReloadBlocker,
  getReloadBlockerSnapshot,
  registerReloadFlusher,
  setReloadBlocker,
  subscribeReloadBlockers,
} from "../../app/reloadBlockers";
import { useAttachments } from "../../contexts/AttachmentsContext";
import { useChat } from "../../contexts/ChatContext";

const bannerStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 12,
  alignItems: "center",
  justifyContent: "space-between",
  padding: "9px max(12px, calc((100% - 960px) / 2))",
  borderBottom: "1px solid #d5b86b",
  background: "#fff8df",
  color: "#4b3d16",
  fontSize: "0.75rem",
};
const copyStyle: CSSProperties = { display: "grid", gap: 2, flex: "1 1 300px", minWidth: 0 };
const detailStyle: CSSProperties = { color: "#756329", overflowWrap: "anywhere" };
const actionsStyle: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 8 };
const buttonStyle: CSSProperties = {
  padding: "5px 9px",
  border: "1px solid #b9a052",
  background: "transparent",
  color: "inherit",
  font: "inherit",
};
const primaryButtonStyle: CSSProperties = {
  ...buttonStyle,
  borderColor: "#4b3d16",
  background: "#4b3d16",
  color: "#fff",
};

export function useBuildUpdateSnapshot() {
  return useSyncExternalStore(
    buildUpdateStore.subscribe,
    buildUpdateStore.getSnapshot,
    buildUpdateStore.getSnapshot,
  );
}

function useReloadReadiness(): void {
  const chat = useChat();
  const attachments = useAttachments();
  const mutating = useIsMutating();
  useEffect(() => {
    setReloadBlocker({
      id: "chat-streaming",
      label: "正在生成回复",
      kind: "transient",
      active: chat.state.requestStatus === "streaming",
    });
    return () => clearReloadBlocker("chat-streaming");
  }, [chat.state.requestStatus]);

  useEffect(() => {
    setReloadBlocker({
      id: "pending-mutations",
      label: `${mutating} 个写操作未完成`,
      kind: "transient",
      active: mutating > 0,
    });
    return () => clearReloadBlocker("pending-mutations");
  }, [mutating]);

  useEffect(() => {
    setReloadBlocker({
      id: "attachment-upload",
      label: "文件上传中",
      kind: "transient",
      active: attachments.state.uploading,
    });
    return () => clearReloadBlocker("attachment-upload");
  }, [attachments.state.uploading]);

  useEffect(() => {
    setReloadBlocker({
      id: "attachment-draft",
      label: `${attachments.readyCount} 个附件尚未发送`,
      kind: "unsaved",
      active: attachments.readyCount > 0,
    });
    return () => clearReloadBlocker("attachment-draft");
  }, [attachments.readyCount]);

  useEffect(() => {
    setReloadBlocker({
      id: "quote-draft",
      label: "引用草稿尚未发送",
      kind: "unsaved",
      active: Boolean(chat.quoteDraft),
    });
    return () => clearReloadBlocker("quote-draft");
  }, [chat.quoteDraft]);

  useEffect(
    () => registerReloadFlusher("conversation", chat.flushConversationPersistence),
    [chat.flushConversationPersistence],
  );

}

export function ReloadReadiness() {
  useReloadReadiness();
  return null;
}

export function BuildUpdateBanner() {
  const snapshot = useBuildUpdateSnapshot();
  const blockers = useSyncExternalStore(
    subscribeReloadBlockers,
    getReloadBlockerSnapshot,
    getReloadBlockerSnapshot,
  );
  const blockerLabels = blockers.map((blocker) => blocker.label);
  const hidden = snapshot.phase === "current"
    || snapshot.deferred
    || !snapshot.targetBuildId;
  if (hidden) return null;

  const blocked = blockerLabels.length > 0;
  const ready = snapshot.phase === "ready"
    || snapshot.phase === "blocked"
    || snapshot.phase === "reload-required";
  const title = blocked
    ? "更新已准备好，完成以下操作后可以重新加载"
    : snapshot.phase === "checking" || snapshot.phase === "installing" || snapshot.phase === "available"
      ? "正在准备 DeepSeek Infra 新构建"
      : snapshot.phase === "activating"
        ? "正在验证新构建"
        : snapshot.phase === "error"
          ? "新构建准备失败"
          : "DeepSeek Infra 新构建已准备好";

  return (
    <aside className="build-update-banner" style={bannerStyle} aria-live="polite">
      <div className="build-update-copy" style={copyStyle}>
        <strong>{title}</strong>
        <span style={detailStyle}>
          {blockerLabels.length
            ? blockerLabels.join(" · ")
            : snapshot.error || `版本 ${snapshot.targetVersion ?? "new"} · ${snapshot.targetBuildId}`}
        </span>
      </div>
      <div className="build-update-actions" style={actionsStyle}>
        {snapshot.phase === "error" && (
          <button style={buttonStyle} type="button" onClick={() => void buildUpdateStore.checkForUpdate()}>重试</button>
        )}
        <button style={buttonStyle} type="button" onClick={() => buildUpdateStore.defer()}>稍后</button>
        <button
          className="primary"
          style={{
            ...primaryButtonStyle,
            opacity: !ready || snapshot.phase === "activating" ? 0.45 : 1,
          }}
          type="button"
          disabled={!ready || snapshot.phase === "activating"}
          onClick={() => void buildUpdateStore.activateWhenReady()}
        >
          {blocked ? "完成后更新" : "更新并重新加载"}
        </button>
      </div>
    </aside>
  );
}
