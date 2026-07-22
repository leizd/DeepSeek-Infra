import { useOverlay } from "../../contexts/OverlayContext";
import { useMemory } from "../../contexts/MemoryContext";
import { Icon } from "../../shared/ui/Icon";
import { runUiAction } from "../../shared/runUiAction";

const CATEGORY_LABELS: Record<string, string> = {
  preference: "偏好",
  project: "项目",
  todo: "待办",
  fact: "事实",
};

export function MemoryDrawer() {
  const overlay = useOverlay();
  const memory = useMemory();
  const open = overlay.activeOverlay === "memory";

  if (!open) return null;
  return (
    <section className="settings-drawer workspace-drawer" role="dialog" aria-modal="true" aria-label="长期记忆">
      <div className="drawer-heading">
        <div>
          <p className="eyebrow">MEMORY</p>
          <h2>长期记忆</h2>
        </div>
        <button type="button" aria-label="关闭记忆面板" onClick={overlay.closeOverlay}><Icon name="close" /></button>
      </div>
      <div className="workspace-toolbar">
        <span className="history-empty-inline">
          共 {memory.memories.length} 条
          {memory.refreshing && <span className="workspace-sync-status" role="status" aria-live="polite">同步中…</span>}
        </span>
        <button
          className="message-action danger"
          type="button"
          disabled={!memory.memories.length || memory.clearing}
          onClick={() => {
            if (window.confirm("确定清空全部长期记忆？")) runUiAction(memory.clear());
          }}
        >
          {memory.clearing ? "清空中…" : "全部清空"}
        </button>
      </div>
      {memory.error && (
        <div className="workspace-error" role="alert">
          <span>{memory.error}</span>
          <button type="button" onClick={() => runUiAction(memory.recover())}>重新同步</button>
        </div>
      )}
      <div className="workspace-list">
        {!memory.memories.length && <p className="history-empty">{memory.loading ? "加载中…" : "还没有长期记忆"}</p>}
        {memory.memories.map((entry) => (
          <div className="workspace-item memory-item" key={entry.id}>
            <div className="memory-entry">
              <small>[{CATEGORY_LABELS[entry.category] ?? entry.category}] {entry.scope}</small>
              <p>{entry.content}</p>
            </div>
            <div className="conversation-item-actions">
              <button
                className="conversation-tool danger"
                type="button"
                title="删除"
                aria-label="删除这条记忆"
                disabled={memory.clearing || memory.isRemovingMemory(entry.id)}
                onClick={() => runUiAction(memory.remove(entry.id))}
              >
                <Icon name="close" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
