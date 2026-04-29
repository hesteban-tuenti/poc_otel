"""
Shared OpenTelemetry setup for all three signals: Traces, Metrics, Logs.

Usage:
    from telemetry import setup_telemetry
    tracer, meter, logger = setup_telemetry("my-service")

Environment variables:
    OTEL_EXPORTER_OTLP_ENDPOINT  OTLP/HTTP base URL (default: http://localhost:4319)
    OTEL_DEBUG                   Set to any value to also print spans to stdout
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter 
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)


def setup_telemetry(service_name: str) -> tuple:
    """
    Initialise TracerProvider, MeterProvider, and LoggerProvider for a service.

    Returns (tracer, meter, logger) ready for use in application code.
    All three providers export via OTLP/HTTP to the OTel Collector.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4319")
    resource = Resource.create({"service.name": service_name})

    # ── Traces ────────────────────────────────────────────────────────────────
    # BatchSpanProcessor buffers spans and flushes in background — correct for
    # production and for sending to a real backend. SimpleSpanProcessor +
    # ConsoleSpanExporter is added when OTEL_DEBUG is set so spans still appear
    # in the terminal during local development without a running backend.
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    if os.getenv("OTEL_DEBUG"):
        tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    # ── Metrics ───────────────────────────────────────────────────────────────
    # PeriodicExportingMetricReader collects all registered instruments every
    # export_interval_millis and pushes them to the exporter. 10s is short
    # enough to see changes quickly in Grafana during a POC demo.
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
        export_interval_millis=10_000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # ── Logs ──────────────────────────────────────────────────────────────────
    # LoggingHandler is a standard Python logging.Handler that bridges stdlib
    # logging to the OTel Logs SDK. When a log record is emitted inside an
    # active span, the handler reads trace.get_current_span() and automatically
    # stamps trace_id and span_id onto the LogRecord before exporting it.
    # This gives trace↔log correlation in Grafana with no extra application code.
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
    )
    set_logger_provider(logger_provider)

    otel_handler = LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # urllib3 logs every HTTP request at DEBUG, including the OTel exporter's own
    # calls to the Collector — which would create new log records, triggering more
    # HTTP calls. Silence it to WARNING to break the loop.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    root_logger.addHandler(otel_handler)

    # Also log to stdout so the terminal is not silent
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    )
    root_logger.addHandler(stream_handler)

    tracer = trace.get_tracer(service_name)
    meter = metrics.get_meter(service_name)
    logger = logging.getLogger(service_name)

    return tracer, meter, logger
