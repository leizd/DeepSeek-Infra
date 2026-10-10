import { useChat } from "../../contexts/ChatContext";
import { useMemory } from "../../contexts/MemoryContext";

export function MemorySuggestionToast() {
  const chat = useChat();
  const memory = useMemory();
  const suggestion = chat.pendingMemorySuggestion;
  if (!suggestion) return null;
  return (
    <div className="memory-suggestion-toast" role="dialog" aria-label="记忆建议">
      <strong>保存这条记忆？</strong>
      <p>{suggestion.content}</p>
      <small>{suggestion.category} · {suggestion.scope}</small>
      {suggestion.conflicts.length > 0 && (
        <div className="memory-suggestion-conflicts">
          <p>与现有记忆冲突：</p>
          <ul>
            {suggestion.conflicts.map((conflict) => (
              <li key={conflict.id}>{conflict.content}{conflict.reason ? `（${conflict.reason}）` : ""}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="memory-suggestion-actions">
        {suggestion.conflicts.length > 0 ? (
          <button
            className="message-action primary"
            type="button"
            disabled={memory.clearing || memory.saving}
            onClick={() => void chat.saveMemorySuggestion(suggestion.conflicts.map((conflict) => conflict.id))}
          >
            替换旧记忆
          </button>
        ) : (
          <button
            className="message-action primary"
            type="button"
            disabled={memory.clearing || memory.saving}
            onClick={() => void chat.saveMemorySuggestion()}
          >
            {memory.saving ? "保存中…" : "保存"}
          </button>
        )}
        <button className="message-action" type="button" onClick={chat.dismissMemorySuggestion}>暂不保存</button>
      </div>
    </div>
  );
}
