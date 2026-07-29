import {
  Client,
  StreamableHTTPClientTransport,
  type CallToolResult,
} from "@modelcontextprotocol/client";

export interface RetriedToolResult {
  result: CallToolResult;
  endpoint: string;
  attempts: number;
}

export class RetryingMcpClient {
  constructor(
    private readonly endpoints: string[],
    private readonly bearerToken?: string,
    private readonly attempts = endpoints.length,
  ) {
    if (endpoints.length === 0) {
      throw new Error("at least one MCP endpoint is required");
    }
  }

  async callTool(name: string, arguments_: Record<string, unknown>): Promise<RetriedToolResult> {
    let lastError: unknown;
    for (let attempt = 0; attempt < this.attempts; attempt += 1) {
      const endpoint = this.endpoints[attempt % this.endpoints.length];
      if (endpoint === undefined) {
        continue;
      }
      const client = new Client({ name: "deepseek-infra-retrying-client", version: "1.0.0" });
      const transport = new StreamableHTTPClientTransport(new URL(endpoint), {
        ...(this.bearerToken === undefined
          ? {}
          : { requestInit: { headers: { authorization: `Bearer ${this.bearerToken}` } } }),
        fetch: async (input, init) => {
          const timeoutSignal = AbortSignal.timeout(3_000);
          const requestSignal = init?.signal;
          return await fetch(input, {
            ...init,
            signal:
              requestSignal === null || requestSignal === undefined
                ? timeoutSignal
                : AbortSignal.any([requestSignal, timeoutSignal]),
          });
        },
      });
      try {
        await client.connect(transport);
        const result = await client.callTool({ name, arguments: arguments_ });
        await client.close();
        return { result, endpoint, attempts: attempt + 1 };
      } catch (error) {
        lastError = error;
        await client.close().catch(() => undefined);
        if (attempt + 1 < this.attempts) {
          await new Promise((resolve) => setTimeout(resolve, Math.min(1_000, 100 * 2 ** attempt)));
        }
      }
    }
    throw lastError instanceof Error ? lastError : new Error(String(lastError));
  }
}

export function parseToolText<T>(result: CallToolResult): T {
  if (result.isError === true) {
    const message = result.content
      .filter((entry) => entry.type === "text")
      .map((entry) => entry.text)
      .join("\n");
    throw new Error(message || "MCP tool returned an error");
  }
  const text = result.content.find((entry) => entry.type === "text");
  if (text === undefined || text.type !== "text") {
    throw new Error("MCP tool response did not contain text");
  }
  return JSON.parse(text.text) as T;
}
