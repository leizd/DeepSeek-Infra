import { useEffect } from "react";

import { useFilePreview } from "../../contexts/FilePreviewContext";

export function ImageLightbox() {
  const preview = useFilePreview();
  const lightbox = preview.lightbox;

  useEffect(() => {
    if (!lightbox) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") preview.closeLightbox();
      if (event.key === "ArrowLeft") preview.stepLightbox(-1);
      if (event.key === "ArrowRight") preview.stepLightbox(1);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [lightbox, preview]);

  if (!lightbox) return null;
  const current = lightbox.items[lightbox.index];
  if (!current) return null;
  const src = current.imagePreview || current.thumbnail || "";
  const many = lightbox.items.length > 1;

  return (
    <div className="image-lightbox" role="dialog" aria-label="图片预览" onClick={preview.closeLightbox}>
      {many && (
        <button
          className="lightbox-nav prev"
          type="button"
          aria-label="上一张"
          disabled={lightbox.index <= 0}
          onClick={(event) => {
            event.stopPropagation();
            preview.stepLightbox(-1);
          }}
        >
          ‹
        </button>
      )}
      <figure onClick={(event) => event.stopPropagation()}>
        <img src={src} alt={current.name} />
        <figcaption>{current.name} · {lightbox.index + 1} / {lightbox.items.length}</figcaption>
      </figure>
      {many && (
        <button
          className="lightbox-nav next"
          type="button"
          aria-label="下一张"
          disabled={lightbox.index >= lightbox.items.length - 1}
          onClick={(event) => {
            event.stopPropagation();
            preview.stepLightbox(1);
          }}
        >
          ›
        </button>
      )}
      <button className="lightbox-close" type="button" aria-label="关闭图片预览" onClick={preview.closeLightbox}>×</button>
    </div>
  );
}
