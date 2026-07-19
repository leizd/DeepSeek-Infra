import { Route, Routes } from "react-router-dom";

import { ChatPage } from "../features/chat/ChatPage";
import { TracePage } from "../features/trace/TracePage";
import { NotFoundPage } from "./NotFoundPage";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<ChatPage />} />
      <Route path="/ui/" element={<ChatPage />} />
      <Route path="/trace/:traceId" element={<TracePage />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
