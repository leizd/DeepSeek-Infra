import { useAttachments } from "../../contexts/AttachmentsContext";
import { useFilePreview } from "../../contexts/FilePreviewContext";
import { formatBytes } from "./attachmentMapper";
import type { PendingAttachment } from "./attachmentReducer";

function statusLabel(item: PendingAttachment): string {
  if (item.status === "uploading") return `上传中 ${item.progress}%`;
  if (item.status === "processing") return "识别中…";
  if (item.status === "error") return item.error ?? "上传失败";
  return formatBytes(item.size);
}

function AttachmentRow({ item }: { item: PendingAttachment }) {
  const attachments = useAttachments();
  const preview = useFilePreview();
  const inFlight = item.status === "uploading" || item.status === "processing";
  return (
    <li className={`attachment-item ${item.status}`} data-attachment-id={item.id}>
      {item.thumbnail ? (
        <img className="attachment-thumb" src={item.thumbnail} alt="" aria-hidden="true" />
      ) : (
        <span className="attachment-kind" aria-hidden="true">{item.kind}</span>
      )}
      <span className="attachment-info">
        <span className="attachment-name" title={item.name}>{item.name}</span>
        <span className="attachment-meta">
          {inFlight && <span className="attachment-progress"><span style={{ width: `${item.progress}%` }} /></span>}
          {statusLabel(item)}
        </span>
      </span>
      <span className="attachment-actions">
        {inFlight && (
          <button type="button" aria-label={`取消上传 ${item.name}`} onClick={() => attachments.cancelUpload()}>取消</button>
        )}
        {item.status === "error" && item.ocrRetryAvailable && (
          <button type="button" aria-label={`OCR 重试 ${item.name}`} onClick={() => attachments.retryWithOcr(item.id)}>OCR 重试</button>
        )}
        {item.status === "ready" && item.attachment && (
          <button type="button" aria-label={`预览附件 ${item.name}`} onClick={() => preview.open(item.attachment as NonNullable<typeof item.attachment>)}>预览</button>
        )}
        {!inFlight && (
          <button type="button" aria-label={`移除附件 ${item.name}`} onClick={() => attachments.removeItem(item.id)}>×</button>
        )}
      </span>
    </li>
  );
}

export function AttachmentList() {
  const attachments = useAttachments();
  if (!attachments.state.items.length) return null;
  return (
    <ul className="attachment-list" aria-label="待发送附件">
      {attachments.state.items.map((item) => <AttachmentRow key={item.id} item={item} />)}
    </ul>
  );
}
