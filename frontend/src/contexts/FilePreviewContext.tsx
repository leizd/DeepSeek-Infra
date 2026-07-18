import { createContext, useContext, type PropsWithChildren } from "react";

import { useFilePreviewController, type FilePreviewController } from "../features/file-reader/useFilePreviewController";

const FilePreviewContext = createContext<FilePreviewController | null>(null);

export function FilePreviewProvider({ children }: PropsWithChildren) {
  const value = useFilePreviewController();
  return <FilePreviewContext.Provider value={value}>{children}</FilePreviewContext.Provider>;
}

export function useFilePreview(): FilePreviewController {
  const value = useContext(FilePreviewContext);
  if (!value) throw new Error("useFilePreview must be used inside FilePreviewProvider");
  return value;
}
