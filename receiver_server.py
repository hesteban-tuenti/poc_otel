import time

from flask import Flask, request
from opentelemetry import baggage, trace
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace import StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator

from telemetry import setup_telemetry

# ── OTel setup ────────────────────────────────────────────────────────────────
SERVICE_NAME = "receiver-service"
tracer, meter, logger = setup_telemetry(SERVICE_NAME)

# ── Metric instruments ────────────────────────────────────────────────────────

# Counts every request received, labelled by the baggage 'request.env' value
requests_received = meter.create_counter(
    "receiver.requests.total",
    description="Total requests received by receiver-service",
)

# Tracks how many requests are currently being processed (in-flight)
active_requests = meter.create_up_down_counter(
    "receiver.requests.active",
    description="Number of requests currently being handled",
)

# Distribution of how many baggage items arrived with each request
baggage_items_histogram = meter.create_histogram(
    "receiver.baggage.items",
    description="Number of baggage items received per request",
    unit="items",
)

# ── Propagator ────────────────────────────────────────────────────────────────
propagator = CompositePropagator([
    TraceContextTextMapPropagator(),
    W3CBaggagePropagator(),
])

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def hello():
    # ── Extract propagation headers ───────────────────────────────────────────
    # Flask normalises incoming header names to title-case (e.g. 'Traceparent').
    # The W3C propagator spec requires lowercase keys, so we normalise here.
    carrier = {k.lower(): v for k, v in request.headers.items()}
    ctx = propagator.extract(carrier=carrier)

    # ── Start a child span linked to the sender's trace ───────────────────────
    with tracer.start_as_current_span("handle_request", context=ctx) as span:
        active_requests.add(1)
        span.set_attribute("http.method", "GET")

        sc = span.get_span_context()
        parent_id = span.parent.span_id if span.parent else None

        # Read baggage values propagated from sender-service
        user_id = baggage.get_baggage("user.id", ctx)
        env     = baggage.get_baggage("request.env", ctx)

        span.set_attribute("baggage.user_id", user_id or "")
        span.set_attribute("baggage.env", env or "")

        logger.info(
            "Received propagated context from sender-service",
            extra={
                "trace_id": f"{sc.trace_id:032x}",
                "span_id": f"{sc.span_id:016x}",
                "parent_span_id": f"{parent_id:016x}" if parent_id else None,
                "baggage.user_id": user_id,
                "baggage.env": env,
            },
        )

        # ── Sub-spans with events and status ─────────────────────────────────
        # Decorated inner functions inherit the active handle_request span as
        # parent automatically — no context= arg needed here, only at service
        # entry points where context crosses a process boundary.

        @tracer.start_as_current_span("validate_request")
        def validate_request():
            s = trace.get_current_span()
            is_valid = bool(user_id and env)
            s.add_event(
                "validation.complete",
                attributes={
                    "user_id_present": bool(user_id),
                    "env_present":     bool(env),
                    "valid":           is_valid,
                },
            )
            s.set_status(
                StatusCode.OK if is_valid else StatusCode.ERROR,
                description="" if is_valid else "Missing required baggage",
            )
            logger.info("Validation result: valid=%s", is_valid)
            return is_valid

        @tracer.start_as_current_span("process_data")
        def process_data():
            s = trace.get_current_span()
            s.add_event("cache.miss", attributes={"cache.key": f"user:{user_id or 'unknown'}"})
            logger.info("Cache miss — processing from source")
            time.sleep(0.01)
            s.add_event("processing.done", attributes={"result.size": 42})
            logger.info("Processing complete")
            s.set_status(StatusCode.OK)

        validate_request()
        process_data()

        # ── Record metrics ────────────────────────────────────────────────────
        baggage_count = sum(1 for v in [user_id, env] if v)
        requests_received.add(1, {"request.env": env or "unknown"})
        baggage_items_histogram.record(baggage_count)

        active_requests.add(-1)
        return f"[{SERVICE_NAME}] trace_id={sc.trace_id:032x} received successfully"


if __name__ == "__main__":
    logger.info("Starting %s on http://127.0.0.1:5001", SERVICE_NAME)
    app.run(port=5001, debug=False)
