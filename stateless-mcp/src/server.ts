import { timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { hostHeaderValidation, toNodeHandler } from "@modelcontextprotocol/node";
import { createMcpHandler } from "@modelcontextprotocol/server";

import { loadConfig } from "./config.js";
import { createServiceMcp } from "./mcp-server.js";
import { RedisTaskStore } from "./redis-task-store.js";
import { initializeTelemetry } from "./telemetry.js";
import { TaskWorker } from "./test-runner.js";

function json(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(body));
}

function authorized(req: IncomingMessage, expected?: string): boolean {
  if (expected === undefined) {
    return true;
  }
  const actual = req.headers.authorization;
  if (actual === undefined || !actual.startsWith("Bearer ")) {
    return false;
  }
  const provided = Buffer.from(actual.slice("Bearer ".length), "utf8");
  const wanted = Buffer.from(expected, "utf8");
  return provided.length === wanted.length && timingSafeEqual(provided, wanted);
}

async function main(): Promise<void> {
  const config = loadConfig();
  const telemetry = initializeTelemetry(config.instanceId);
  const store = await RedisTaskStore.connect(config.redisUrl, config.redisPrefix);
  const worker = new TaskWorker(store, config);
  const workerPromise = worker.start().catch((error: unknown) => {
    console.error("task_worker_failed", error);
    process.exitCode = 1;
  });
  const mcpHandler = createMcpHandler(
    () => createServiceMcp(store, config, telemetry),
    {
      legacy: "stateless",
      onerror: (error) => {
        console.error("mcp_request_failed", error);
      },
    },
  );
  const nodeMcpHandler = toNodeHandler(mcpHandler);
  const validateHost = hostHeaderValidation(config.allowedHostnames);

  const httpServer = createServer((req, res) => {
    if (!validateHost(req, res)) {
      return;
    }
    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    if (url.pathname === "/healthz") {
      json(res, 200, { status: "ok", instanceId: config.instanceId });
      return;
    }
    if (url.pathname === "/readyz") {
      void store
        .get("readiness-probe")
        .then(() => json(res, 200, { status: "ready", instanceId: config.instanceId }))
        .catch((error: unknown) =>
          json(res, 503, {
            status: "not-ready",
            error: error instanceof Error ? error.message : String(error),
          }),
        );
      return;
    }
    if (url.pathname === "/instance") {
      json(res, 200, { instanceId: config.instanceId, clientSessionState: "none" });
      return;
    }
    if (url.pathname !== "/mcp") {
      json(res, 404, { error: "not found" });
      return;
    }
    if (!authorized(req, config.authToken)) {
      res.setHeader("www-authenticate", 'Bearer realm="stateless-mcp"');
      json(res, 401, { error: "unauthorized" });
      return;
    }
    void nodeMcpHandler(req as unknown as Parameters<typeof nodeMcpHandler>[0], res);
  });

  let shuttingDown = false;
  const shutdown = async (signal: string): Promise<void> => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    console.log("shutdown_started", { signal, instanceId: config.instanceId });
    await worker.stop();
    await new Promise<void>((resolve, reject) => {
      httpServer.close((error) => {
        if (error) {
          reject(error);
        } else {
          resolve();
        }
      });
    });
    await workerPromise;
    await mcpHandler.close();
    await store.close();
    await telemetry.shutdown();
  };
  process.once("SIGINT", () => void shutdown("SIGINT"));
  process.once("SIGTERM", () => void shutdown("SIGTERM"));

  httpServer.listen(config.port, config.host, () => {
    console.log("stateless_mcp_listening", {
      host: config.host,
      port: config.port,
      instanceId: config.instanceId,
      workspaceRoot: config.workspaceRoot,
    });
  });
}

await main();
