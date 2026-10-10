import { useCallback } from "react";

import { FILE_READER_CHUNK_COUNT, loadFileChunk } from "../../api/fileReaderApi";
import { useChat } from "../../contexts/ChatContext";
import { useFilePreview } from "../../contexts/FilePreviewContext";
import type { ChatMessage } from "../../domain/chat/types";
import {
  chunkWindowStart,
  isWebCitationId,
  parseFileCitation,
  resolveWebCitationUrl,
} from "./citations";

export function useOpenCitation(messages: readonly ChatMessage[]): (message: ChatMessage) => (citationId: string) => void {
  const chat = useChat();
  const preview = useFilePreview();

  return useCallback(
    (message: ChatMessage) => (citationId: string) => {
      if (isWebCitationId(citationId)) {
        const url = resolveWebCitationUrl(message.search, citationId);
        if (url) {
          window.open(url, "_blank", "noopener,noreferrer");
        } else {
          chat.notify("没有找到这个来源对应的链接");
        }
        return;
      }

      const fileCitation = parseFileCitation(citationId);
      if (!fileCitation) return;
      const messageIndex = messages.findIndex((item) => item.id === message.id);
      const userMessage = messages
        .slice(0, messageIndex >= 0 ? messageIndex : messages.length)
        .reverse()
        .find((item) => item.role === "user");
      const attachment = userMessage?.attachments[fileCitation.fileIndex - 1];
      if (!attachment?.fileId) {
        chat.notify("没有找到这个引用对应的文件");
        return;
      }
      void loadFileChunk({ fileId: attachment.fileId, projectId: attachment.projectId ?? "" }, fileCitation.chunkIndex)
        .then((chunk) => {
          preview.open(
            {
              ...attachment,
              name: `${attachment.name} · ${citationId.toUpperCase()}`,
              text: chunk.text || attachment.text,
              preview: chunk.text || attachment.preview,
            },
            { chunkStart: chunkWindowStart(fileCitation.chunkIndex, FILE_READER_CHUNK_COUNT) },
          );
        })
        .catch((reason: unknown) => {
          chat.notify(reason instanceof Error && reason.message ? reason.message : "读取引用片段失败");
        });
    },
    [chat, messages, preview],
  );
}
