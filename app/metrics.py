"""The Prometheus metrics the service exports."""

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
)

from app.slots import Slots

# Without this every counter and histogram also exports a *_created timestamp
# series, which nothing in this project reads.
disable_created_metrics()

OUTCOMES = ("ok", "error", "rejected")

# Upper bounds in seconds, shared by both histograms: from well under the default
# model time of 0.1 s to a queue that has backed up for many model times.
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    def __init__(self, slots: Slots, git_sha: str) -> None:
        # A registry per application instead of the process-wide default, so that
        # every application object, and with it every test, starts from zero.
        self.registry = CollectorRegistry()

        self.requests = Counter(
            "inference_requests_total",
            "Inference requests by outcome: ok, error (a 5xx) or rejected (a 429).",
            ["outcome"],
            registry=self.registry,
        )
        self.request_duration = Histogram(
            "inference_request_duration_seconds",
            "Time from the arrival of a request to its response, by outcome.",
            ["outcome"],
            buckets=BUCKETS,
            registry=self.registry,
        )
        self.queue_wait = Histogram(
            "inference_queue_wait_seconds",
            "Time an admitted request waited for a model slot.",
            buckets=BUCKETS,
            registry=self.registry,
        )
        # Export every outcome from the start, at zero. A series that first appears
        # with its first event gives rate() no earlier sample to count that event from.
        for outcome in OUTCOMES:
            self.requests.labels(outcome)
            self.request_duration.labels(outcome)

        # These two read the live counts when scraped, so there is no copy to keep in step.
        Gauge(
            "inference_requests_in_flight",
            "Requests holding a model slot.",
            registry=self.registry,
        ).set_function(lambda: slots.in_flight)
        Gauge(
            "inference_requests_queued",
            "Requests waiting for a model slot.",
            registry=self.registry,
        ).set_function(lambda: slots.queued)
        Gauge(
            "inference_model_slots",
            "Requests the model can serve at the same time.",
            registry=self.registry,
        ).set(slots.total)
        Gauge(
            "inference_build_info",
            "The build that is running. The value is always 1.",
            ["git_sha"],
            registry=self.registry,
        ).labels(git_sha).set(1)
