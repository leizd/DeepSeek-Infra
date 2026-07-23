import "../workspace/workspace-optional.css";
import "./memory.css";

import { MemoryListProvider } from "../../contexts/MemoryListContext";
import { MemoryDrawer } from "./MemoryDrawer";

export default function MemoryFeature() {
  return (
    <MemoryListProvider>
      <MemoryDrawer />
    </MemoryListProvider>
  );
}
