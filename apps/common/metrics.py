from __future__ import annotations

from prometheus_client import Counter, Histogram, start_http_server

JOBS_COMPLETED = Counter("elastic_jobs_completed_total", "Jobs that reached COMPLETED.")
JOBS_FAILED = Counter("elastic_jobs_failed_total", "Jobs that reached FAILED.")
JOBS_INTERRUPTED = Counter(
    "elastic_jobs_interrupted_total",
    "Attempts that ended in INTERRUPTED (shutdown, lease loss, or retryable failure).",
)
LEASE_CLAIMS = Counter("elastic_lease_claims_total", "Successful job claims.")
LEASE_STEALS = Counter(
    "elastic_lease_steals_total",
    "Claims that took over a PROCESSING job whose lease had expired.",
)
LEASE_RENEWAL_FAILURES = Counter(
    "elastic_lease_renewal_failures_total",
    "Heartbeats that discovered the lease was no longer ours.",
)
VISIBILITY_EXTENSIONS = Counter(
    "elastic_visibility_extensions_total",
    "SQS visibility timeout extensions performed while processing.",
)
TRANSCODE_SECONDS = Histogram(
    "elastic_transcode_duration_seconds",
    "Wall-clock ffmpeg transcode duration.",
    buckets=(5, 15, 30, 60, 120, 300, 600, 1200, 3600),
)
RECONCILER_REQUEUED = Counter(
    "elastic_reconciler_requeued_total",
    "Jobs the reconciler pushed back into the queue.",
    ["reason"],
)
RECONCILER_EXPIRED = Counter(
    "elastic_reconciler_expired_total",
    "Jobs the reconciler failed because the upload never arrived.",
)


def start_metrics_server(port: int) -> None:
    start_http_server(port)
