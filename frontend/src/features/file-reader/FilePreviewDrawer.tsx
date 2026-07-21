import { filePageImageUrl, fileSourceUrl, type FileReference } from "../../api/fileReaderApi";
import { useFilePreview } from "../../contexts/FilePreviewContext";
import { Icon } from "../../shared/ui/Icon";
import { formatBytes } from "../attachments/attachmentMapper";
import { supportsOriginalPreview } from "./filePreviewReducer";

function referenceOf(fileId: string | undefined, projectId: string | undefined): FileReference | null {
  return fileId ? { fileId, projectId: projectId ?? "" } : null;
}

export function FilePreviewDrawer() {
  const preview = useFilePreview();
  const { state } = preview;
  const attachment = state.attachment;
  if (!attachment) return null;

  const reference = referenceOf(attachment.fileId, attachment.projectId);
  const canShowOriginal = supportsOriginalPreview(attachment);
  const isPdf = attachment.kind === "pdf";
  const isImage = attachment.kind === "image";
  const meta = [attachment.kind ?? "file", formatBytes(attachment.size), attachment.pageCount ? `${attachment.pageCount} 页` : ""]
    .filter(Boolean)
    .join(" · ");

  return (
    <aside className="file-preview-drawer" aria-label="文件预览">
      <header className="drawer-heading">
        <div>
          <p className="eyebrow">FILE PREVIEW</p>
          <h2>{attachment.name}</h2>
          <p className="file-preview-meta">{meta}</p>
        </div>
        <button type="button" aria-label="关闭文件预览" onClick={preview.close}><Icon name="close" /></button>
      </header>

      {canShowOriginal && (
        <div className="file-reader-toolbar">
          {state.mode === "original" ? (
            <button type="button" onClick={() => preview.setMode("extracted")}>查看文本</button>
          ) : (
            <button type="button" onClick={() => preview.setMode("original")}>查看原文</button>
          )}
          {isPdf && state.mode === "original" && (
            <>
              <button type="button" disabled={state.page <= 1} onClick={() => preview.setPage(state.page - 1)}>上一页</button>
              <span className="file-reader-position">{state.page} / {preview.pageCount}</span>
              <button type="button" disabled={state.page >= preview.pageCount} onClick={() => preview.setPage(state.page + 1)}>下一页</button>
            </>
          )}
          {state.mode === "extracted" && state.window && (
            <>
              <button type="button" disabled={!state.window.hasPrevious || state.loading} onClick={preview.previousWindow}>上一段</button>
              <span className="file-reader-position">{state.window.chunkStart}–{state.window.chunkEnd} / {state.window.totalChunks}</span>
              <button type="button" disabled={!state.window.hasNext || state.loading} onClick={preview.nextWindow}>下一段</button>
            </>
          )}
          {reference && (
            <a className="file-reader-download" href={fileSourceUrl(reference, true)} download={attachment.name}>下载</a>
          )}
        </div>
      )}

      <div className="file-preview-body">
        {state.error && <p className="message-error">{state.error}</p>}
        {state.mode === "legacy" && (
          <pre className="file-reader-legacy">{attachment.preview || attachment.text || "（无可用预览）"}</pre>
        )}
        {state.mode === "extracted" && (
          state.loading && !state.chunks.length ? (
            <p className="file-preview-loading">正在读取文件内容…</p>
          ) : (
            <pre className="file-reader-text">{state.chunks.map((chunk) => chunk.text).join("\n\n") || (state.loading ? "" : "（本段无文本）")}</pre>
          )
        )}
        {state.mode === "original" && reference && isPdf && (
          <img className="file-original-page" src={filePageImageUrl(reference, state.page)} alt={`第 ${state.page} 页`} />
        )}
        {state.mode === "original" && reference && isImage && (
          <img className="file-original-image" src={fileSourceUrl(reference)} alt={attachment.name} />
        )}
        {state.mode === "original" && reference && !isPdf && !isImage && (
          <iframe className="file-original-frame" src={fileSourceUrl(reference)} title={attachment.name} sandbox="" />
        )}
      </div>
    </aside>
  );
}
