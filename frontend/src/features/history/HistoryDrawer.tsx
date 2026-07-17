import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { ConversationList } from "./ConversationList";

export function HistoryDrawer() {
  const chat = useChat();
  const overlay = useOverlay();
  return (
    <aside className={overlay.activeOverlay === "history" ? "history-drawer mobile-open" : "history-drawer"} aria-label="对话历史">
      <div className="history-brand">
        <div className="brand-mark">DS</div>
        <div><strong>DeepSeek Infra</strong><small>React 普通聊天纵切</small></div>
        <button className="mobile-close" type="button" aria-label="关闭历史" onClick={overlay.closeOverlay}>×</button>
      </div>
      <button
        className="new-chat-button"
        type="button"
        disabled={chat.state.requestStatus === "streaming"}
        onClick={() => {
          chat.newConversation();
          overlay.closeOverlay();
        }}
      >
        ＋ 新对话
      </button>
      <ConversationList />
      <div className="history-footer">
        <span>历史仅保存在本机浏览器</span>
        <a href="/">Legacy 工作区</a>
      </div>
    </aside>
  );
}
