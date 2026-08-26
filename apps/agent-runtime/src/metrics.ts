import { Counter, Gauge, Histogram, Registry, collectDefaultMetrics } from "prom-client";

export class RuntimeMetrics {
  readonly registry = new Registry();
  readonly activeRuns: Gauge;
  readonly requests: Counter;
  readonly duration: Histogram;
  readonly providerRequests: Counter;
  readonly toolCalls: Counter;
  readonly toolDuration: Histogram;
  readonly limits: Counter;
  readonly cleanupFailures: Counter;

  constructor() {
    collectDefaultMetrics({ register: this.registry, prefix: "lumen_agent_runtime_" });
    this.activeRuns = new Gauge({
      name: "lumen_agent_runtime_active_runs",
      help: "Current Agent Runtime executions.",
      registers: [this.registry],
    });
    this.requests = new Counter({
      name: "lumen_agent_runtime_requests_total",
      help: "Agent Runtime requests by bounded outcome.",
      labelNames: ["outcome"],
      registers: [this.registry],
    });
    this.duration = new Histogram({
      name: "lumen_agent_runtime_request_duration_seconds",
      help: "Agent Runtime request duration.",
      labelNames: ["outcome"],
      buckets: [0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300],
      registers: [this.registry],
    });
    this.providerRequests = new Counter({
      name: "lumen_agent_runtime_provider_requests_total",
      help: "Provider dispatches and responses.",
      labelNames: ["phase", "status"],
      registers: [this.registry],
    });
    this.toolCalls = new Counter({
      name: "lumen_agent_runtime_tool_calls_total",
      help: "Lumen tool calls by terminal result.",
      labelNames: ["name", "mode", "status"],
      registers: [this.registry],
    });
    this.toolDuration = new Histogram({
      name: "lumen_agent_runtime_tool_duration_seconds",
      help: "Lumen tool duration by name and terminal result.",
      labelNames: ["name", "mode", "status"],
      buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
      registers: [this.registry],
    });
    this.limits = new Counter({
      name: "lumen_agent_runtime_limits_total",
      help: "Agent Runtime limit stops.",
      labelNames: ["reason"],
      registers: [this.registry],
    });
    this.cleanupFailures = new Counter({
      name: "lumen_agent_runtime_cleanup_failures_total",
      help: "Agent Runtime cleanup failures by bounded resource slot.",
      labelNames: ["resource"],
      registers: [this.registry],
    });
  }
}
