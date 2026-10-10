export const IMAGE_THUMBNAIL_MAX_SIDE = 96;
export const IMAGE_PREVIEW_MAX_SIDE = 1600;
export const IMAGE_THUMBNAIL_QUALITY = 0.78;
export const IMAGE_PREVIEW_QUALITY = 0.84;
export const MAX_LOCAL_IMAGE_PREVIEW_BYTES = 30_000_000;

export interface ImagePreviewPair {
  thumbnail: string;
  imagePreview: string;
}

export function scaledDimensions(width: number, height: number, maxSide: number): { width: number; height: number } {
  if (width <= 0 || height <= 0 || maxSide <= 0) return { width: 0, height: 0 };
  const scale = Math.min(1, maxSide / Math.max(width, height));
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : "");
    reader.onerror = () => reject(new Error("无法读取图片文件"));
    reader.readAsDataURL(file);
  });
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("图片解码失败"));
    image.src = src;
  });
}

function jpegDataUrl(image: HTMLImageElement, maxSide: number, quality: number): string {
  const { width, height } = scaledDimensions(image.naturalWidth, image.naturalHeight, maxSide);
  if (!width || !height) return "";
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) return "";
  context.drawImage(image, 0, 0, width, height);
  try {
    return canvas.toDataURL("image/jpeg", quality);
  } catch {
    return "";
  }
}

export async function createImagePreviews(file: File): Promise<ImagePreviewPair | null> {
  if (typeof document === "undefined" || typeof FileReader === "undefined") return null;
  if (file.size > MAX_LOCAL_IMAGE_PREVIEW_BYTES) return null;
  try {
    const dataUrl = await readAsDataUrl(file);
    const image = await loadImage(dataUrl);
    const thumbnail = jpegDataUrl(image, IMAGE_THUMBNAIL_MAX_SIDE, IMAGE_THUMBNAIL_QUALITY);
    const imagePreview = jpegDataUrl(image, IMAGE_PREVIEW_MAX_SIDE, IMAGE_PREVIEW_QUALITY);
    if (!thumbnail || !imagePreview) return null;
    return { thumbnail, imagePreview };
  } catch {
    return null;
  }
}
