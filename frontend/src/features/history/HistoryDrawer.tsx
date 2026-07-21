import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { ConversationList } from "./ConversationList";
import { Icon } from "../../shared/ui/Icon";

export function HistoryDrawer() {
  const chat = useChat();
  const overlay = useOverlay();
  return (
    <aside className={overlay.activeOverlay === "history" ? "history-drawer mobile-open" : "history-drawer"} aria-label="对话历史">
      <div className="history-brand">
        <div className="brand-mark">DS</div>
        <div><strong>DeepSeek Infra</strong><small>Local AI Runtime</small></div>
        <button className="mobile-close" type="button" aria-label="关闭历史" onClick={overlay.closeOverlay}><Icon name="close" /></button>
      </div>
      <button
        className="new-chat-button swap-btn"
        type="button"
        disabled={chat.state.requestStatus === "streaming"}
        onClick={() => {
          chat.newConversation();
          overlay.closeOverlay();
        }}
      >
        <span className="swap"><span className="a">新对话</span><span className="b">↗</span></span>
      </button>
      <div className="workspace-nav">
        <button type="button" onClick={() => overlay.openOverlay("projects")}>项目</button>
        <button type="button" onClick={() => overlay.openOverlay("skills")}>技能</button>
        <button type="button" onClick={() => overlay.openOverlay("memory")}>记忆</button>
        <button type="button" onClick={() => overlay.openOverlay("reminders")}>提醒</button>
      </div>
      <ConversationList />
      <div className="history-footer">
        <span>历史仅保存在本机浏览器</span>
      </div>
    </aside>
  );
}
