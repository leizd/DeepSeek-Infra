import { timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { hostHeaderValidation, toNodeHandler } from "@modelcontextprotocol/node";
import { createMcpHandler } from "@modelcontextprotocol/server";

import { loadConfig } from "./config.js";
import { createServiceMcp } from "./mcp-server.js";
import { RedisTaskStore } from "./redis-task-store.js";
import { initializeTelemetry } from "./telemetry.js";
import { TaskWorker } from "./test-runner.js";
import { parseBackupSnapshot } from "./task-store.js";

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

async function readBody(req: IncomingMessage, limit = 64 * 1024 * 1024): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += value.length;
    if (size > limit) throw new Error("request body is too large");
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function* streamRequestBody(req: IncomingMessage): AsyncGenerator<string, void, void> {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    yield decoder.decode(buffer, { stream: true });
  }
  const tail = decoder.decode();
  if (tail.length > 0) {
    yield tail;
  }
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
    if (url.pathname.startsWith("/internal/")) {
      const token = config.internalBackupToken;
      if (token === undefined || !authorized(req, token)) {
        json(res, token === undefined ? 404 : 401, { error: token === undefined ? "not found" : "unauthorized" });
        return;
      }
      void (async () => {
        if (req.method === "GET" && url.pathname === "/internal/backups/capabilities") {
          json(res, 200, await store.backupCapabilities());
          return;
        }
        if (req.method === "POST" && url.pathname === "/internal/backups/prepare") {
          const body = JSON.parse(await readBody(req)) as { backupId?: string };
          if (typeof body.backupId !== "string" || body.backupId.length < 8) throw new Error("backupId is required");
          json(res, 200, await store.prepareBackup(body.backupId, Date.now()));
          return;
        }
        const backupMatch = /^\/internal\/backups\/([^/]+)\/(stream|release)$/u.exec(url.pathname);
        if (backupMatch !== null && backupMatch[1] !== undefined && backupMatch[2] === "stream" && req.method === "GET") {
          const snapshot = await store.exportBackup(backupMatch[1]);
          res.writeHead(200, { "content-type": "application/x-ndjson", "cache-control": "no-store" });
          res.end(snapshot);
          return;
        }
        if (backupMatch !== null && backupMatch[1] !== undefined && backupMatch[2] === "release" && req.method === "POST") {
          await store.releaseBackup(backupMatch[1]);
          json(res, 200, { ok: true, backupId: backupMatch[1] });
          return;
        }
        if (req.method === "POST" && url.pathname === "/internal/restores/inspect") {
          const snapshot = await readBody(req);
          const parsed = parseBackupSnapshot(snapshot);
          json(res, 200, { ok: true, schemaVersion: 1, tasks: parsed.tasks.length });
          return;
        }
        const restoreMatch = /^\/internal\/restores\/([^/]+)(?:\/(prepare|commit-intent|commit|complete|abort|apply))?$/u.exec(
          url.pathname,
        );
        if (restoreMatch !== null && restoreMatch[1] !== undefined) {
          const restoreId = restoreMatch[1];
          const action = restoreMatch[2];
          if (req.method === "GET" && action === undefined) {
            const journal = await store.restoreStatus(restoreId);
            if (journal === null) {
              json(res, 404, { error: "not found" });
              return;
            }
            json(res, 200, journal);
            return;
          }
          if (req.method === "POST" && action === "prepare") {
            const transactionDigest = req.headers["x-transaction-digest"];
            if (typeof transactionDigest !== "string" || transactionDigest.length < 8) {
              throw new Error("X-Transaction-Digest is required");
            }
            const expectedDigest = req.headers["x-content-sha256"];
            const journal = await store.prepareRestore(
              restoreId,
              transactionDigest,
              streamRequestBody(req),
              Date.now(),
            );
            if (typeof expectedDigest === "string" && expectedDigest.length > 0 && expectedDigest !== journal.sourceDigest) {
              await store.abortRestore(restoreId, Date.now());
              throw new Error("snapshot digest does not match X-Content-SHA256");
            }
            json(res, 200, journal);
            return;
          }
          if (req.method === "POST" && action === "commit-intent") {
            const body = JSON.parse(await readBody(req)) as { transactionDigest?: string };
            if (typeof body.transactionDigest !== "string" || body.transactionDigest.length < 8) {
              throw new Error("transactionDigest is required");
            }
            json(res, 200, await store.commitRestoreIntent(restoreId, body.transactionDigest, Date.now()));
            return;
          }
          if (req.method === "POST" && action === "commit") {
            const body = JSON.parse(await readBody(req)) as { transactionDigest?: string };
            if (typeof body.transactionDigest !== "string" || body.transactionDigest.length < 8) {
              throw new Error("transactionDigest is required");
            }
            json(res, 200, await store.commitRestore(restoreId, body.transactionDigest, Date.now()));
            return;
          }
          if (req.method === "POST" && action === "complete") {
            json(res, 200, await store.completeRestore(restoreId, Date.now()));
            return;
          }
          if (req.method === "POST" && action === "abort") {
            json(res, 200, await store.abortRestore(restoreId, Date.now()));
            return;
          }
          if (req.method === "POST" && action === "apply") {
            json(res, 200, await store.restoreBackup(restoreId, await readBody(req), Date.now()));
            return;
          }
        }
        json(res, 404, { error: "not found" });
      })().catch((error: unknown) => {
        json(res, 409, { error: error instanceof Error ? error.message : String(error) });
      });
      return;
    }
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
