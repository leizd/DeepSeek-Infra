import {
  metrics,
  SpanStatusCode,
  trace,
  type Attributes,
  type Counter,
  type Histogram,
  type Tracer,
} from "@opentelemetry/api";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { MeterProvider, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import { BatchSpanProcessor, NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import {
  ATTR_SERVICE_INSTANCE_ID,
  ATTR_SERVICE_NAME,
  ATTR_SERVICE_VERSION,
} from "@opentelemetry/semantic-conventions";

export interface Telemetry {
  runTool<T>(toolName: string, operation: () => Promise<T>): Promise<T>;
  shutdown(): Promise<void>;
}

class ToolTelemetry implements Telemetry {
  constructor(
    private readonly tracer: Tracer,
    private readonly calls: Counter,
    private readonly failures: Counter,
    private readonly duration: Histogram,
    private readonly instanceId: string,
    private readonly shutdownProviders: () => Promise<void>,
  ) {}

  async runTool<T>(toolName: string, operation: () => Promise<T>): Promise<T> {
    const attributes: Attributes = {
      "mcp.tool.name": toolName,
      "service.instance.id": this.instanceId,
    };
    const started = performance.now();
    return await this.tracer.startActiveSpan(`mcp.tool.${toolName}`, { attributes }, async (span) => {
      try {
        const result = await operation();
        this.calls.add(1, { ...attributes, "mcp.tool.status": "ok" });
        span.setStatus({ code: SpanStatusCode.OK });
        return result;
      } catch (error) {
        this.calls.add(1, { ...attributes, "mcp.tool.status": "error" });
        this.failures.add(1, attributes);
        span.recordException(error instanceof Error ? error : new Error(String(error)));
        span.setStatus({
          code: SpanStatusCode.ERROR,
          message: error instanceof Error ? error.message : String(error),
        });
        throw error;
      } finally {
        this.duration.record(performance.now() - started, attributes);
        span.end();
      }
    });
  }

  async shutdown(): Promise<void> {
    await this.shutdownProviders();
  }
}

export function initializeTelemetry(instanceId: string): Telemetry {
  const endpoint = process.env.OTEL_EXPORTER_OTLP_ENDPOINT?.replace(/\/$/u, "");
  const resource = resourceFromAttributes({
    [ATTR_SERVICE_NAME]: "deepseek-infra-stateless-mcp",
    [ATTR_SERVICE_VERSION]: "1.0.0",
    [ATTR_SERVICE_INSTANCE_ID]: instanceId,
  });
  let meterProvider: MeterProvider | null = null;
  let tracerProvider: NodeTracerProvider | null = null;

  if (endpoint !== undefined && endpoint.length > 0) {
    const metricReader = new PeriodicExportingMetricReader({
      exporter: new OTLPMetricExporter({
        url: process.env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT || `${endpoint}/v1/metrics`,
      }),
      exportIntervalMillis: 5_000,
    });
    meterProvider = new MeterProvider({ resource, readers: [metricReader] });
    metrics.setGlobalMeterProvider(meterProvider);

    tracerProvider = new NodeTracerProvider({
      resource,
      spanProcessors: [
        new BatchSpanProcessor(
          new OTLPTraceExporter({
            url: process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT || `${endpoint}/v1/traces`,
          }),
        ),
      ],
    });
    tracerProvider.register();
  }

  const meter = metrics.getMeter("deepseek-infra-stateless-mcp", "1.0.0");
  const tracer = trace.getTracer("deepseek-infra-stateless-mcp", "1.0.0");
  return new ToolTelemetry(
    tracer,
    meter.createCounter("mcp.tool.calls", { description: "MCP tool calls" }),
    meter.createCounter("mcp.tool.failures", { description: "Failed MCP tool calls" }),
    meter.createHistogram("mcp.tool.duration", {
      description: "MCP tool execution duration",
      unit: "ms",
    }),
    instanceId,
    async () => {
      await Promise.all([meterProvider?.shutdown(), tracerProvider?.shutdown()]);
    },
  );
}
