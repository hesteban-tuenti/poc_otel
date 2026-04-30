# Development Guide

Instructions for running, developing, and understanding this OpenTelemetry POC.

## Running the POC

### 1. Start the backend

```bash
docker-compose up -d
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Start both services (receiver first)

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

Grafana is at `http://localhost:3000` — see `README.md` for what to check per signal.

**Debug mode** (prints spans to terminal without needing a running backend):

```bash
OTEL_DEBUG=true uv run python sending_server.py
```

---

## Architecture

Three OTel signals (Traces, Metrics, Logs) flow from two Flask services through a central OTel Collector to a `grafana/otel-lgtm` backend.

```
sender-service (5002) ──HTTP──▶ receiver-service (5001)
       │                               │
   OTLP/HTTP :4319               OTLP/HTTP :4319
       └──────── OTel Collector ───────┘
                      │ OTLP/HTTP
               grafana/otel-lgtm
           Tempo │ Loki │ Prometheus
                 Grafana UI :3000
```

**`telemetry.py`** — shared setup module. Both services call `setup_telemetry(service_name)` which returns `(tracer, meter, logger)` and registers all three providers. OTel logs use the `LoggingHandler` bridge: attaching it to Python's stdlib `logging` module causes `logger.info(...)` calls inside an active span to automatically carry `trace_id` and `span_id`.

**`sending_server.py`** — creates a root span, sets W3C baggage, injects `traceparent` + `baggage` headers into the outbound HTTP call, records request counter + latency histogram.

**`receiver_server.py`** — extracts the propagation headers, reconstructs trace context, starts a child span linked to the sender's span, records counters and histograms. Within that child span it defines two decorated inner functions that each become a grandchild sub-span:

- `validate_request` — checks that expected baggage values are present; adds a `validation.complete` span event with boolean attributes; sets `StatusCode.OK` or `StatusCode.ERROR` depending on the result.
- `process_data` — simulates a cache miss and I/O delay; adds `cache.miss` and `processing.done` span events; sets `StatusCode.OK`.

Both use `@tracer.start_as_current_span` as a **decorator** (rather than a `with` block). The span is created when the function is called and closed automatically when it returns. Inside each function, `trace.get_current_span()` is used to access the active sub-span, since the decorator form does not expose a span reference via `as`. Each function also emits log records via `logger.info(...)` — because `LoggingHandler` reads the active span at call time, those log lines are stamped with the sub-span's `span_id`, not the parent `handle_request` span's.

The resulting trace has four spans under one `trace_id`:
```
sender:handle_request
  └─ receiver:handle_request
       ├─ receiver:validate_request  (events: validation.complete)
       └─ receiver:process_data      (events: cache.miss, processing.done)
```

See `README.md` for the full OTel component map, per-signal flow diagrams, and Grafana verification steps.
