import { useEffect, useState } from "react";

import { useActivity } from "../../contexts/ActivityContext";
import { useChat } from "../../contexts/ChatContext";
import { useOverlay } from "../../contexts/OverlayContext";
import { agentExecutionReport } from "../../domain/chat/agentTimeline";
import type { ChatMessage } from "../../domain/chat/types";
import { MarkdownContent } from "../../shared/markdown/MarkdownContent";
import { Icon } from "../../shared/ui/Icon";
import { AgentTimeline } from "../agent-run/AgentTimeline";
import { SearchBlock } from "../citations/SearchBlock";
import { copyTextToClipboard } from "../chat/messageActions";
import { activitySummaryText, messageHasActivity } from "./activitySummary";

const WIDE_MEDIA_QUERY = "(min-width: 960px)";
const AUTO_OPEN_WIDTH = 960;

function useAutoOpen(messages: readonly ChatMessage[]) {
  const activity = useActivity();
  const overlay = useOverlay();
  useEffect(() => {
    if (overlay.activeOverlay) return;
    if (typeof window !== "undefined" && window.innerWidth < AUTO_OPEN_WIDTH) return;
    const candidate = [...messages].reverse().find((message) => message.streaming && messageHasActivity(message));
    if (candidate) activity.autoOpen(candidate.id);
  }, [activity, messages, overlay.activeOverlay]);
}

function CopyReportButton({ message }: { message: ChatMessage }) {
  const [copied, setCopied] = useState(false);
  const report = agentExecutionReport(message);
  if (!report) return null;
  return (
    <button
      className="message-action"
      type="button"
      onClick={() => {
        void copyTextToClipboard(report).then((ok) => {
          if (!ok) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1_200);
        });
      }}
    >
      {copied ? "已复制" : "复制 Agent 过程"}
    </button>
  );
}

export function ActivityDrawer() {
  const activity = useActivity();
  const chat = useChat();
  const [now, setNow] = useState(() => Date.now());
  useAutoOpen(chat.messages);

  const message = chat.messages.find((item) => item.id === activity.openMessageId) ?? null;

  useEffect(() => {
    if (!message?.streaming) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [message?.streaming]);

  if (!message) return null;
  return (
    <aside className="activity-panel" aria-label="思考与活动">
      <header className="activity-panel-header">
        <div>
          <p className="eyebrow">ACTIVITY</p>
          <h2>{activitySummaryText(message, now)}</h2>
        </div>
        <div className="activity-panel-tools">
          <CopyReportButton message={message} />
          <button type="button" aria-label="关闭活动面板" onClick={activity.closeActivity}><Icon name="close" /></button>
        </div>
      </header>
      <div className="activity-panel-body">
        {message.systemNotes.map((note, index) => <p className="system-note" key={index}>{note}</p>)}
        {message.search && <SearchBlock search={message.search} streaming={message.streaming} />}
        <AgentTimeline message={message} />
        {message.reasoning && (
          <div className="activity-reasoning">
            <h3>推理过程</h3>
            <MarkdownContent content={message.reasoning} />
          </div>
        )}
        {!messageHasActivity(message) && <p className="file-preview-loading">这条消息暂无可展示的活动。</p>}
      </div>
    </aside>
  );
}

export function ActivityTrigger({ message }: { message: ChatMessage }) {
  const activity = useActivity();
  const [now] = useState(() => Date.now());
  if (!messageHasActivity(message)) return null;
  return (
    <button className="activity-trigger" type="button" onClick={() => activity.openActivity(message.id)}>
      {activitySummaryText(message, now)}
    </button>
  );
}

export { WIDE_MEDIA_QUERY };
