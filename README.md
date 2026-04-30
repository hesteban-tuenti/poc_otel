# OTel POC — Scope, Technical Reference & Verification Guide

## Goal

Demonstrate all three OpenTelemetry observability pillars across two Python microservices:

| Pillar | What it answers | Stored in |
|--------|----------------|-----------|
| **Traces** | What happened, in what order, how long each step took | Tempo |
| **Metrics** | How many times, how fast, how much — aggregated over time | Prometheus/Mimir |
| **Logs** | What the code said while it was running, correlated to a trace | Loki |

The critical learning objective is **correlation**: a single request produces a trace, metrics, and log lines that all share the same `trace_id`, so you can pivot between them in Grafana.

---

## Infrastructure

```
curl
 │
 ▼
sender-service (port 5002)                receiver-service (port 5001)
 │  creates root span                       │  extracts traceparent + baggage
 │  sets baggage                            │  creates child span (same trace_id)
 │  injects traceparent + baggage headers   │  creates sub-spans with events + status
 │  records metrics                         │  records metrics
 │  logs with trace_id stamped              │  logs with span_id of the active sub-span
 └──── HTTP GET with propagation headers ───┘
 │                                          │
 │  OTLP/HTTP → localhost:4319              │  OTLP/HTTP → localhost:4319
 └──────────────────┬───────────────────────┘
                    ▼
          OTel Collector (host:4319)
          otel-collector-config.yaml
          receivers:  otlp/http
          processors: memory_limiter, batch
          exporters:  otlphttp/grafana, debug
                    │
                    │  OTLP/HTTP → lgtm:4318 (Docker network)
                    ▼
          grafana/otel-lgtm (host:4318, :3000)
          ┌──────────────────────────────────┐
          │  Tempo      ← traces             │
          │  Loki       ← logs               │
          │  Prometheus ← metrics            │
          │  Grafana    → unified UI :3000   │
          └──────────────────────────────────┘
```

**Port mapping note:** The LGTM container binds to host port 4318 for its OTLP receiver. The Collector is therefore mapped to host port 4319 (`4319:4318` in docker-compose) so both can run simultaneously. Python services send to `localhost:4319`.

---

## OTel SDK Components

### Providers — one per signal, set globally at startup

| Component | Signal | What it manages |
|-----------|--------|----------------|
| `TracerProvider` | Traces | Factory for `Tracer` objects; holds span processors and the resource |
| `MeterProvider` | Metrics | Factory for `Meter` objects; holds metric readers and the resource |
| `LoggerProvider` | Logs | Factory for OTel loggers; holds log record processors |

All three are configured in `telemetry.py` and registered globally via `trace.set_tracer_provider(...)`, `metrics.set_meter_provider(...)`, `set_logger_provider(...)`.

### Processors / Readers — buffer and forward signal data

| Component | Signal | Behaviour |
|-----------|--------|-----------|
| `BatchSpanProcessor` | Traces | Buffers finished spans in memory, flushes to exporter in background batches |
| `PeriodicExportingMetricReader` | Metrics | Collects all instrument values every `export_interval_millis` (10s in this POC) and pushes to exporter |
| `BatchLogRecordProcessor` | Logs | Same as BatchSpanProcessor but for log records |
| `SimpleSpanProcessor` | Traces | Exports each span synchronously as it closes — only enabled when `OTEL_DEBUG=true`; used for local terminal inspection |

### Exporters — send data over the wire

| Component | Signal | Endpoint | Protocol |
|-----------|--------|----------|----------|
| `OTLPSpanExporter` | Traces | `/v1/traces` | OTLP/HTTP (protobuf) |
| `OTLPMetricExporter` | Metrics | `/v1/metrics` | OTLP/HTTP (protobuf) |
| `OTLPLogExporter` | Logs | `/v1/logs` | OTLP/HTTP (protobuf) |

All three exporters talk to the OTel Collector at `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4319`).

### Propagation — cross-service context linking

| Component | HTTP header | Carries |
|-----------|-------------|---------|
| `TraceContextTextMapPropagator` | `traceparent` | `trace_id` + `span_id` + sampling flag (W3C Trace Context spec) |
| `W3CBaggagePropagator` | `baggage` | Arbitrary key/value pairs (W3C Baggage spec) |
| `CompositePropagator` | both | Wraps both propagators; `inject()`/`extract()` in one call |

### The LoggingHandler bridge

```
logger.info("message")                   ← normal Python stdlib call
    └─ Python logging dispatches to all handlers
         └─ LoggingHandler (OTel bridge)
              └─ reads trace.get_current_span()
                   └─ stamps trace_id + span_id onto the LogRecord
                        └─ BatchLogRecordProcessor → OTLPLogExporter → Collector
```

No application code change is needed for correlation. As long as `logger.info(...)` is called inside an active span, the `trace_id` and `span_id` appear automatically on the exported log record.

### Resource

`Resource.create({"service.name": "sender-service"})` attaches the service name to every span, metric data point, and log record from that process. In the backend this becomes a label/attribute used to filter by service in Grafana.

---

## Process Boundaries — What Runs Where

There are three separate processes involved. Understanding which OTel components live in each is important because two of them have a component called "batch processor" that are completely independent.

### Inside each Python service (sender-service, receiver-service)

All of the following run **in-process**, as background threads of the Flask app:

| Component | Runs as | Role |
|---|---|---|
| `TracerProvider` / `MeterProvider` / `LoggerProvider` | In-process object | Factories; hold processors and the resource |
| `BatchSpanProcessor` | Background thread | Buffers finished spans, flushes to exporter |
| `BatchLogRecordProcessor` | Background thread | Buffers log records, flushes to exporter |
| `PeriodicExportingMetricReader` | Background thread | Wakes every 10s, collects metric values, pushes to exporter |
| `OTLPSpanExporter` / `OTLPMetricExporter` / `OTLPLogExporter` | In-process | Makes HTTP POST to the Collector |
| `CompositePropagator` | In-process, at request time | `inject`/`extract` on business HTTP headers |
| `LoggingHandler` | In-process, at log-call time | Reads active span, stamps `trace_id`/`span_id` onto log records |

### Inside the OTel Collector (separate Go process / Docker container)

A completely separate process — nothing to do with the Python SDK:

| Component | Role |
|---|---|
| `otlp` receiver | Listens on `:4318`, accepts OTLP/HTTP POSTs from services |
| `memory_limiter` processor | Drops data if memory pressure is too high |
| `batch` processor | Re-batches before forwarding to reduce connections to backend |
| `otlphttp/grafana` exporter | Forwards to LGTM container on `:4318` (internal Docker network) |
| `debug` exporter | Prints summary lines to Collector stdout |

### Inside the LGTM container (separate Docker container)

| Component | Stores / serves |
|---|---|
| Tempo | Traces |
| Loki | Logs |
| Prometheus | Metrics |
| Grafana | Unified query UI on `:3000` |

### The two batch processors are independent

The naming overlap is a common source of confusion:

```
[service process — Python]              [Collector process — Go]
BatchSpanProcessor ──OTLP/HTTP──▶  batch processor ──OTLP/HTTP──▶  Tempo
(SDK, in-process)                   (Collector config)
buffers spans inside the app         re-batches for the backend
before the network hop               before the second network hop
```

Both buffer data to avoid blocking on network I/O, but they operate at different hops and are configured independently.

---

## Signal Flow Per Pillar

### Traces

```
tracer.start_as_current_span("handle_request")           ← sender, creates root span
  └─ SDK creates Span with: trace_id, span_id, start_time, attributes
       └─ Span is stored in the OTel context (thread-local)
            └─ propagator.inject(headers) writes traceparent header
                 └─ HTTP GET to receiver-service
                      └─ propagator.extract(carrier) reconstructs ctx
                           └─ start_as_current_span("handle_request", context=ctx)
                                └─ child span: same trace_id, parent_span_id = sender's span_id

                                     └─ @tracer.start_as_current_span("validate_request")
                                          └─ grandchild span, parent = handle_request
                                               └─ span.add_event("validation.complete", {...})
                                               └─ span.set_status(StatusCode.OK | ERROR)
                                               └─ closed automatically when function returns

                                     └─ @tracer.start_as_current_span("process_data")
                                          └─ grandchild span, parent = handle_request
                                               └─ span.add_event("cache.miss", {...})
                                               └─ span.add_event("processing.done", {...})
                                               └─ span.set_status(StatusCode.OK)
                                               └─ closed automatically when function returns

  └─ each span closes → BatchSpanProcessor receives it
       └─ OTLPSpanExporter POSTs to /v1/traces
            └─ Collector forwards to lgtm:4318
                 └─ Tempo stores all 4 spans under the same trace_id
                      └─ Grafana Explore → Tempo → 3-level waterfall
```

**Span events** are timestamped annotations attached to a span — think of them as structured log entries scoped to a specific operation. They appear in Tempo's Events tab when you click a span.

**Span status** (`StatusCode.OK` / `StatusCode.ERROR`) is a machine-readable outcome separate from span attributes. Backends use it to mark failed spans in red in the waterfall view.

**Decorator vs context manager:** `@tracer.start_as_current_span` is the decorator form of the same API. The span inherits its parent from the active context (thread-local) automatically — no `context=` arg needed. `context=ctx` is only required at service entry points where context crosses a process boundary via `propagator.extract()`.

### Metrics

```
request_counter.add(1, {"http.status_code": "200"})    ← called in request handler
latency_histogram.record(elapsed_ms, {...})

  PeriodicExportingMetricReader wakes every 10s
    └─ collects current values of all instruments
         └─ OTLPMetricExporter POSTs to /v1/metrics
              └─ Collector forwards to lgtm:4318
                   └─ Prometheus/Mimir stores the time-series
                        └─ Grafana Explore → Prometheus → query sender_requests_total
```

### Logs

```
logger.info("Sending request", extra={"trace_id": ...})
  └─ Python logging dispatches to LoggingHandler
       └─ handler reads active span → stamps trace_id + span_id
            └─ BatchLogRecordProcessor buffers the LogRecord
                 └─ OTLPLogExporter POSTs to /v1/logs
                      └─ Collector forwards to lgtm:4318
                           └─ Loki stores the log line (indexed by service.name)
                                └─ Grafana Explore → Loki → {service_name="sender-service"}
```

---

## Running the POC

### 1. Start the backend

```bash
docker-compose up -d
```

Wait ~15 seconds for LGTM to initialise. Verify:

```bash
docker-compose logs otel-collector   # should show "Everything is ready"
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Start both services

Open two terminals. The receiver must be running before you trigger the sender.

```bash
# Terminal 1
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319 uv run python receiver_server.py

# Terminal 2
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319 uv run python sending_server.py
```

### 4. Trigger a request

```bash
curl http://127.0.0.1:5002/
```

Run it a few times to generate enough data for metrics to be visible.

---

## Debugging via the OTel Collector

The `debug` exporter is enabled in all three signal pipelines in `otel-collector-config.yaml`. It prints a summary line to the Collector's stdout each time a batch is flushed.

```bash
docker-compose logs -f otel-collector
```

### Expected output

**Metrics — appear every ~10 seconds, even with no traffic:**

```
otel-collector-1  | 2026-04-29T08:45:44.728Z  info  Metrics  {
    "otelcol.signal": "metrics",
    "resource metrics": 1,
    "metrics": 12,
    "data points": 13
}
```

The 12 metrics / 13 data points are a combination of the SDK's internal instruments (exported spans count, active spans, etc.) and the custom `sender.*` / `receiver.*` instruments defined in the services.

**Traces and Logs — appear only after a `curl` request:**

```
otel-collector-1  | 2026-04-29T08:33:10.102Z  info  Traces  {
    "otelcol.signal": "traces",
    "resource spans": 2,
    "spans": 4
}
otel-collector-1  | 2026-04-29T08:33:10.215Z  info  Logs  {
    "otelcol.signal": "logs",
    "resource logs": 2,
    "log records": 6
}
```

`resource spans: 2` means one resource (each service) contributed spans. `spans: 4` is the total: `sender:handle_request` + `receiver:handle_request` + `receiver:validate_request` + `receiver:process_data`. `log records: 6`: 2 from sender, 1 from receiver's handle_request, 1 from validate_request, 2 from process_data.

### Why metrics appear constantly but traces/logs only on request

| Signal | Export trigger |
|--------|---------------|
| Metrics | `PeriodicExportingMetricReader` pushes every 10s unconditionally |
| Traces | `BatchSpanProcessor` flushes when a span finishes (or the buffer fills) |
| Logs | `BatchLogRecordProcessor` flushes when log records are buffered (or the buffer fills) |

### Increasing verbosity

To see the full payload of every span, metric, and log record passing through the Collector, change `verbosity: basic` to `verbosity: detailed` in `otel-collector-config.yaml` and restart:

```bash
docker-compose restart otel-collector
```

Revert to `basic` once done — `detailed` is very noisy in steady state.

---

## Verification in Grafana

Open `http://localhost:3000` (no login required — anonymous access is enabled).

### Traces → Grafana Explore → datasource: Tempo

1. Left sidebar → compass icon (Explore)
2. Datasource dropdown → **Tempo**
3. Query type: **Search** → Service Name: `sender-service`
4. Click a trace → expand the waterfall

**What to verify:**
- Four spans appear in the same trace: `sender:handle_request` → `receiver:handle_request` → `receiver:validate_request` + `receiver:process_data`
- The receiver's `handle_request` span's `parent_span_id` equals the sender span's `span_id`
- Click `validate_request` → **Events** tab → `validation.complete` event with `user_id_present`, `env_present`, `valid` attributes
- Click `process_data` → **Events** tab → `cache.miss` and `processing.done` events
- `validate_request` shows `StatusCode=OK`; to see `ERROR`, remove baggage from `sending_server.py` and retry
- Span attributes (`http.method`, `peer.service`, `baggage.user_id`, etc.) are visible on each span

### Metrics → Grafana Explore → datasource: Prometheus

1. Explore → datasource: **Prometheus**
2. Query: `sender_requests_total` → Run query

**What to verify:**
- Counter increments with each `curl`
- Query `sender_request_duration_ms_bucket` to see histogram buckets
- Query `receiver_requests_total` labelled by `request_env`

### Logs → Grafana Explore → datasource: Loki

1. Explore → datasource: **Loki**
2. Label filter: `service_name` = `sender-service`
3. Run query

**What to verify:**
- Log lines appear with timestamps
- Each line has a `trace_id` field in the parsed labels
- The `trace_id` value matches what you see in Tempo for the same request

### Trace ↔ Log correlation

1. In Tempo, open a trace and click a span
2. Look for a **"Logs"** panel or **"Related logs"** button (appears when Tempo datasource is linked to Loki)
3. Correlated log lines for that exact span should appear inline

---


---

## Key Concepts

**Why `BatchSpanProcessor` in production, `SimpleSpanProcessor` for debug?**
`BatchSpanProcessor` exports asynchronously and is efficient, but spans appear in Grafana with a small delay (up to the flush interval). `SimpleSpanProcessor` exports synchronously on span close, making terminal output immediate — useful when developing without a backend. Enable it with `OTEL_DEBUG=true`.

**Why 10-second metric export interval?**
The default is 60 seconds. For a POC demo, 10 seconds means you see counter changes in Grafana quickly without waiting a minute. In production, 60 seconds is more appropriate.

**What does OTLP/HTTP mean?**
"OTLP/HTTP" stacks two things — the data format and the transport:

```
OTLP  /  HTTP
 │         └─ transport: plain HTTP/1.1 POST requests
 └─ data format: OpenTelemetry Protocol, serialized as Protocol Buffers (protobuf)
```

OTLP defines the data model (what fields a span, metric, or log record must have), the binary encoding (protobuf), and the endpoint paths (`/v1/traces`, `/v1/metrics`, `/v1/logs`). "HTTP" is just the transport layer. It is not "plain HTTP" — the body is binary protobuf, not JSON or form data. The alternative transport is OTLP/gRPC, which uses the same protobuf encoding over gRPC (HTTP/2) instead.

| | OTLP/HTTP | OTLP/gRPC |
|---|---|---|
| Transport | HTTP/1.1 POST | gRPC (HTTP/2) |
| Encoding | Protobuf (default) or JSON | Protobuf only |
| Port convention | 4318 | 4317 |
| Needs C extension | No | Yes (`grpcio`) |

**Why OTLP/HTTP instead of OTLP/gRPC?**
HTTP/protobuf requires no C-extension (`grpcio`), installs faster with `uv`, and works through any load balancer or proxy without HTTP/2 negotiation. The Collector accepts both; OTLP/HTTP is the lower-friction choice for local development.

**Why does the receiver lowercase header keys?**
Flask normalises incoming HTTP headers to title-case (`Traceparent`, `Baggage`). The W3C propagator spec mandates lowercase keys when extracting. The one-liner `{k.lower(): v for k, v in request.headers.items()}` fixes the mismatch.

**What are span events?**
`span.add_event(name, attributes={...})` records a timestamped annotation on the span — a discrete moment in time with structured data, but not a separate span. Unlike spans, events have no duration. They are useful for marking cache hits/misses, retries, checkpoints, or any point of interest during an operation. They appear in Tempo under the **Events** tab when you click a span.

**What is span status?**
`span.set_status(StatusCode.OK | StatusCode.ERROR, description="...")` sets a machine-readable outcome on the span. The default is `StatusCode.UNSET`, which backends treat as "no error" but it is distinct from an explicit `OK`. Setting `ERROR` causes Tempo to highlight the span in red and enables alerting rules based on error rate. If an exception propagates through a `start_as_current_span` block, the status is set to `ERROR` automatically (controlled by the `set_status_on_exception=True` default).

**Why do logs inside sub-spans have a different `span_id` than the parent span?**
`LoggingHandler` calls `trace.get_current_span()` at the moment `logger.info(...)` is called. Inside `validate_request` or `process_data`, the active span is the sub-span — so the log record is stamped with the sub-span's `span_id`, not `handle_request`'s. All logs from the same request share the same `trace_id`, but you can filter by `span_id` in Loki to see only what happened inside a specific operation.

**How the Collector routes signals to different backends**
The endpoint path the SDK posts to is the routing key — the Collector reads it and directs the payload into the matching pipeline, without inspecting the protobuf body:

```
SDK (Python process)
  OTLPSpanExporter   POST :4319/v1/traces  ─┐
  OTLPMetricExporter POST :4319/v1/metrics  ├─▶ Collector OTLP receiver (reads endpoint path)
  OTLPLogExporter    POST :4319/v1/logs    ─┘         │
                                                       │  routes by signal type into pipeline
                                                       ▼
                                            otel-collector-config.yaml
                                            pipelines:
                                              traces:  receivers[otlp] → processors → exporters
                                              metrics: receivers[otlp] → processors → exporters
                                              logs:    receivers[otlp] → processors → exporters
                                                       │
                                            otlphttp/grafana exporter
                                                       │
                          ┌────────────────────────────┼────────────────────────────┐
                          ▼                            ▼                            ▼
               POST :4318/v1/traces        POST :4318/v1/metrics        POST :4318/v1/logs
                          │                            │                            │
                        Tempo                    Prometheus                       Loki
```

The pipeline config in `otel-collector-config.yaml` is also where you control which backends receive which signals. To send traces to Jaeger instead of Tempo, add a `jaeger` exporter to the `traces` pipeline only — metrics and logs are unaffected and no Python code changes.

**Swapping backends**
Because all services talk only to the OTel Collector, replacing the backend requires only changing the `exporters` block in `otel-collector-config.yaml` and restarting the Collector container. The Python code is unchanged. To evaluate Jaeger instead of Tempo, add a `jaeger` exporter to the Collector config and point a Grafana datasource at it.

**Propagator vs OTLP exporter — two separate mechanisms**
These solve different problems and use different channels:

| | Propagator (`inject`/`extract`) | OTLP Exporter |
|-|--------------------------------|---------------|
| **Purpose** | Let the receiver *join* the same trace | Send finished span data to the backend |
| **Channel** | The existing business HTTP request (GET `/`) | A separate HTTP connection to the Collector |
| **Format** | Plain string header — `traceparent: 00-<trace_id>-<span_id>-01` | Protobuf body — POST to `/v1/traces` |
| **Read by** | The other service's OTel SDK on `extract()` | OTel Collector → Tempo |
| **When** | At request time, before the outbound call | After the span closes, when the batch processor flushes |

Concretely, per request two independent network flows occur:

```
sender-service
  │
  ├─── BUSINESS REQUEST ──────────────────────────▶ receiver-service :5001
  │    GET http://127.0.0.1:5001/
  │    Headers:
  │      traceparent: 00-<trace_id>-<span_id>-01    ← propagator wrote this
  │      baggage: user.id=user-42, request.env=dev
  │
  └─── TELEMETRY EXPORT ──────────────────────────▶ OTel Collector :4319
       POST http://localhost:4319/v1/traces          ← OTLP exporter, separate connection
       Body: protobuf-encoded finished span
```

The propagator ensures both spans share the same `trace_id` and have a parent–child relationship. The OTLP exporter is what delivers both spans to Tempo so you can view the waterfall in Grafana.
