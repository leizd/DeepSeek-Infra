import { createMcpHandler } from "@modelcontextprotocol/server";

import type { ServiceConfig } from "./config.js";
import { createServiceMcp } from "./mcp-server.js";
import type { TaskStore } from "./task-store.js";
import type { Telemetry } from "./telemetry.js";

export function createServiceMcpHandler(
  store: TaskStore,
  config: ServiceConfig,
  telemetry: Telemetry,
  onerror: (error: unknown) => void,
) {
  return createMcpHandler(() => createServiceMcp(store, config, telemetry), {
    legacy: "stateless",
    // Only the MCP route is bounded here; backup/restore streaming is separate.
    maxRequestBodySize: 1024 * 1024,
    onerror,
  });
}
