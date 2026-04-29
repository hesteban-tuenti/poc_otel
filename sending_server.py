import time

from flask import Flask
import requests
from opentelemetry import baggage
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator

from telemetry import setup_telemetry

# ── OTel setup ────────────────────────────────────────────────────────────────
SERVICE_NAME = "sender-service"
tracer, meter, logger = setup_telemetry(SERVICE_NAME)

# ── Metric instruments ────────────────────────────────────────────────────────
# Created once at module level; thread-safe, reused across requests.

# Counts every outbound call to receiver-service, labelled by HTTP status code
request_counter = meter.create_counter(
    "sender.requests.total",
    description="Total outbound requests sent to receiver-service",
)

# Tracks end-to-end roundtrip time for each call to receiver-service
latency_histogram = meter.create_histogram(
    "sender.request.duration_ms",
    description="Roundtrip latency of outbound call to receiver-service",
    unit="ms",
)

# Tracks how many baggage items are attached per request (goes up and down if
# items are added/removed across requests; useful as a gauge-like instrument)
baggage_items_counter = meter.create_up_down_counter(
    "sender.baggage.items",
    description="Number of baggage key/value pairs injected per request",
)

# ── Propagator ────────────────────────────────────────────────────────────────
propagator = CompositePropagator([
    TraceContextTextMapPropagator(),   # injects/extracts 'traceparent' header
    W3CBaggagePropagator(),            # injects/extracts 'baggage' header
])

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def hello():
    with tracer.start_as_current_span("handle_request") as span:
        span.set_attribute("http.method", "GET")
        span.set_attribute("peer.service", "receiver-service")

        # ── Baggage ───────────────────────────────────────────────────────────
        # baggage.set_baggage returns a new context derived from the current one
        # (which already holds the active span), so both span context and baggage
        # are present in ctx and will be injected together.
        ctx = baggage.set_baggage("user.id", "user-42")
        ctx = baggage.set_baggage("request.env", "dev", context=ctx)

        # ── Inject propagation headers ────────────────────────────────────────
        headers: dict[str, str] = {}
        propagator.inject(headers, context=ctx)

        sc = span.get_span_context()
        logger.info(
            "Sending request to receiver-service",
            extra={
                "trace_id": f"{sc.trace_id:032x}",
                "span_id": f"{sc.span_id:016x}",
                "injected_headers": list(headers.keys()),
            },
        )

        # ── Outbound HTTP call ────────────────────────────────────────────────
        start = time.monotonic()
        response = requests.get("http://127.0.0.1:5001/", headers=headers)
        elapsed_ms = (time.monotonic() - start) * 1000

        span.set_attribute("http.status_code", response.status_code)

        # ── Record metrics ────────────────────────────────────────────────────
        labels = {"http.status_code": str(response.status_code)}
        request_counter.add(1, labels)
        latency_histogram.record(elapsed_ms, {"peer.service": "receiver-service"})
        baggage_items_counter.add(2)   # user.id + request.env

        logger.info(
            "Received response from receiver-service",
            extra={"status_code": response.status_code, "duration_ms": round(elapsed_ms, 2)},
        )

        return f"[{SERVICE_NAME}] {response.text}"


if __name__ == "__main__":
    logger.info("Starting %s on http://127.0.0.1:5002", SERVICE_NAME)
    app.run(port=5002, debug=False)
