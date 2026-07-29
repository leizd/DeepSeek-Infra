import { McpServer } from "@modelcontextprotocol/server";
import { z } from "zod";

import { searchCode } from "./code-search.js";
import type { ServiceConfig } from "./config.js";
import type { TaskStore } from "./task-store.js";
import type { Telemetry } from "./telemetry.js";

function response(value: unknown) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(value) }],
  };
}

function errorResponse(error: unknown) {
  return {
    content: [
      {
        type: "text" as const,
        text: error instanceof Error ? error.message : String(error),
      },
    ],
    isError: true,
  };
}

export function createServiceMcp(
  store: TaskStore,
  config: ServiceConfig,
  telemetry: Telemetry,
): McpServer {
  const server = new McpServer(
    { name: "deepseek-infra-stateless-mcp", version: "1.0.0" },
    {
      capabilities: {
        tools: { listChanged: false },
      },
    },
  );

  server.registerTool(
    "server_info",
    {
      title: "Stateless MCP server information",
      description: "Returns the serving instance and statelessness contract.",
      inputSchema: z.object({}),
      annotations: { readOnlyHint: true, idempotentHint: true },
    },
    async () =>
      await telemetry.runTool("server_info", async () =>
        response({
          instanceId: config.instanceId,
          clientSessionState: "none",
          durableTaskState: "redis",
        }),
      ),
  );

  server.registerTool(
    "code_search",
    {
      title: "Search code",
      description: "Searches literal text with ripgrep inside the configured workspace.",
      inputSchema: z.object({
        query: z.string().min(1).max(500),
        path: z.string().min(1).default("."),
        glob: z.string().min(1).max(200).optional(),
        maxResults: z.number().int().min(1).max(200).default(50),
      }),
      annotations: { readOnlyHint: true, idempotentHint: true },
    },
    async (input) => {
      try {
        return await telemetry.runTool("code_search", async () =>
          response(
            await searchCode(
              config.workspaceRoot,
              {
                query: input.query,
                path: input.path,
                maxResults: input.maxResults,
                ...(input.glob === undefined ? {} : { glob: input.glob }),
              },
              config.maxOutputBytes,
            ),
          ),
        );
      } catch (error) {
        return errorResponse(error);
      }
    },
  );

  server.registerTool(
    "start_test_run",
    {
      title: "Start test run",
      description:
        "Queues a durable pytest task. idempotencyKey is required and safely deduplicates retries.",
      inputSchema: z.object({
        idempotencyKey: z.string().min(8).max(200),
        target: z.string().min(1).max(500),
        keyword: z.string().min(1).max(200).optional(),
        markers: z.string().min(1).max(200).optional(),
        timeoutSeconds: z.number().int().min(1).max(3_600).default(600),
      }),
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    },
    async ({ idempotencyKey, target, keyword, markers, timeoutSeconds }) => {
      try {
        return await telemetry.runTool("start_test_run", async () => {
          const result = await store.createOrGet({
            idempotencyKey,
            arguments: {
              target,
              ...(keyword === undefined ? {} : { keyword }),
              ...(markers === undefined ? {} : { markers }),
              timeoutSeconds,
            },
            now: Date.now(),
          });
          return response({
            taskId: result.task.id,
            status: result.task.status,
            attempts: result.task.attempts,
            deduplicated: result.deduplicated,
          });
        });
      } catch (error) {
        return errorResponse(error);
      }
    },
  );

  server.registerTool(
    "get_task",
    {
      title: "Get test task",
      description: "Gets the durable status and result of a test task.",
      inputSchema: z.object({ taskId: z.string().uuid() }),
      annotations: { readOnlyHint: true, idempotentHint: true },
    },
    async ({ taskId }) => {
      try {
        return await telemetry.runTool("get_task", async () => {
          const task = await store.get(taskId);
          if (task === null) {
            throw new Error(`task ${taskId} was not found`);
          }
          return response(task);
        });
      } catch (error) {
        return errorResponse(error);
      }
    },
  );

  server.registerTool(
    "query_logs",
    {
      title: "Query test logs",
      description: "Queries persisted stdout and stderr from a durable test task.",
      inputSchema: z.object({
        taskId: z.string().uuid(),
        stream: z.enum(["all", "stdout", "stderr"]).default("all"),
        contains: z.string().max(200).optional(),
        maxLines: z.number().int().min(1).max(1_000).default(200),
      }),
      annotations: { readOnlyHint: true, idempotentHint: true },
    },
    async ({ taskId, stream, contains, maxLines }) => {
      try {
        return await telemetry.runTool("query_logs", async () => {
          const task = await store.get(taskId);
          if (task === null) {
            throw new Error(`task ${taskId} was not found`);
          }
          const entries: Array<{ stream: "stdout" | "stderr"; line: string }> = [];
          if (stream !== "stderr") {
            entries.push(
              ...task.stdout.split(/\r?\n/u).map((line) => ({ stream: "stdout" as const, line })),
            );
          }
          if (stream !== "stdout") {
            entries.push(
              ...task.stderr.split(/\r?\n/u).map((line) => ({ stream: "stderr" as const, line })),
            );
          }
          const filtered = entries
            .filter((entry) => entry.line.length > 0)
            .filter((entry) => contains === undefined || entry.line.includes(contains))
            .slice(-maxLines);
          return response({
            taskId,
            status: task.status,
            lines: filtered,
            truncated: filtered.length === maxLines,
          });
        });
      } catch (error) {
        return errorResponse(error);
      }
    },
  );

  return server;
}
