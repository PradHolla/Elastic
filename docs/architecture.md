# Architecture

## Purpose

Elastic is a distributed video-processing system that prioritizes interruption tolerance over feature breadth. The first implementation is not trying to be a complete media platform. It is trying to prove that a queue-driven worker fleet can process long-running jobs on EKS Spot while keeping durable, comprehensible job state in DynamoDB and exposing that state through a browser dashboard.

The system target is a single-rendition transcode pipeline with direct-to-S3 upload, SQS-based decoupling, KEDA autoscaling, and safe retry behavior under duplicate delivery and worker interruption.

## System Boundary

### In Scope

- FastAPI control plane for job creation and job status lookup
- Browser dashboard for upload and monitoring
- Direct client upload to S3 using presigned instructions
- S3 object-created events sent to SQS
- Worker pods on EKS consuming queue messages
- KEDA scale-to-zero based on SQS depth
- Spot-backed nodes with interruption-aware workers
- DynamoDB as the durable source of truth for job state
- One output artifact at `1080p`

### Out of Scope

- Step Functions orchestration
- Multiple renditions or fan-out DAGs
- HLS packaging
- Resume-from-checkpoint transcoding
- Authentication and multi-tenant authorization
- Human-facing web UI beyond the minimal dashboard
- Claims of exactly-once processing

## Components And Responsibilities

### Client Dashboard / CLI

- Calls `POST /jobs`
- Receives upload instructions and `job_id`
- Uploads the source object directly to S3
- Polls `GET /jobs/{job_id}` for status
- Can also use `GET /jobs` to render a queue-like job list

### FastAPI Control Plane

- Creates the job record in DynamoDB
- Generates presigned upload instructions: a single PUT with a signed
  `Content-Length` for small files, or a presigned multipart upload (part
  URLs plus complete/abort endpoints) at and above the multipart threshold
- Returns deterministic object keys to the client
- Exposes job status from DynamoDB
- Runs the reconciliation sweeper as a background thread

### S3

- Stores input video under `inputs/{job_id}/source`
- Stores final output under `outputs/{job_id}/final/1080p.mp4`
- Emits `ObjectCreated` notifications after upload completion

### SQS Standard Queue

- Buffers upload-complete events
- Decouples ingestion from compute
- Enables KEDA-driven scaling
- Allows message redelivery after interruption or failure

### DynamoDB Jobs Table

- Stores canonical job status, attempt count, last error, and the current lease (`lease_owner`, `lease_expires_at`)
- Prevents conflicting transitions through conditional writes; the attempt count doubles as the fencing token
- Serves "recent jobs by state" through the `status-updated_at-index` GSI (no table scans)

### EKS Worker Pods

- Consume SQS messages
- Normalize incoming event payload into the internal job message shape
- Claim a job through a lease: an atomic conditional write that succeeds only if the job is `QUEUED` or a previous owner's lease has expired
- Download the source file (streamed to disk), run FFmpeg, upload the output, and finalize status with a fenced write
- Heartbeat while working: extend SQS visibility and renew the DynamoDB lease on the same cadence; abandon the attempt if the lease is lost
- Handle `SIGTERM` by stopping FFmpeg, marking the job `INTERRUPTED`, and releasing the message immediately for fast retry

### KEDA

- Watches SQS queue depth
- Scales the worker deployment from zero upward
- Scales back down when backlog clears

### Spot Node Layer

- Runs the worker pods on interruptible compute
- Makes interruption recovery a real operational concern rather than a hypothetical one

## Data Flow

1. The client calls `POST /jobs` with source metadata and the target preset.
2. The API inserts a job row in DynamoDB and returns upload instructions plus a deterministic `job_id`.
3. The API leaves the job in `UPLOADING` after upload instructions are successfully returned.
4. The client uploads the source object directly to S3 at `inputs/{job_id}/source`.
5. S3 emits an `ObjectCreated` notification into SQS.
6. A worker receives the SQS message, extracts `job_id` from the deterministic key, and normalizes the event into the canonical internal message shape.
7. The worker performs a conditional DynamoDB update to record that the upload has been observed and the job is now `QUEUED`. If the worker can immediately claim the job, this state may be brief.
8. The worker claims the job by transitioning it to `PROCESSING` and incrementing `attempt_count`.
9. The worker downloads the source object, writes temporary local output, runs FFmpeg, and periodically extends SQS visibility timeout.
10. On success, the worker uploads the result to `outputs/{job_id}/final/1080p.mp4`, updates DynamoDB to `COMPLETED`, and only then deletes the SQS message.
11. On interruption or retryable failure, the worker records `INTERRUPTED` or `FAILED` as appropriate and allows the message to reappear or be dead-lettered according to queue policy.

## Job Ownership: Leases And Fencing Tokens

A bare `PROCESSING` flag cannot distinguish "a worker is actively transcoding"
from "a worker died holding this job." Elastic resolves that with a lease and a
fencing token, both stored on the job record:

- **Claiming.** `claim_job` is a single DynamoDB conditional write: *make me
  the owner if the job is `QUEUED`, or if it is `PROCESSING` and
  `lease_expires_at` has passed*. Because DynamoDB evaluates the condition and
  applies the write atomically, two racing workers can never both win; the
  loser gets a conditional-check failure and walks away.
- **Heartbeating.** The owner renews the lease on the same cadence as the SQS
  visibility extension. A healthy worker is therefore always ahead of the
  expiry; a dead one stops renewing and its lease lapses.
- **Stealing.** When a message is redelivered and the job is `PROCESSING` with
  an expired lease, the new worker claims it directly — no operator
  intervention. A `PROCESSING` job with a live lease is left alone.
- **Fencing.** Each claim increments `attempt_count`, which acts as the
  fencing token. Every subsequent write by that worker (lease renewal, the
  final `COMPLETED`/`FAILED`/`INTERRUPTED` transition) is conditioned on
  `attempt_count` still matching its claim. A zombie worker — paused,
  partitioned, or resumed after its lease was stolen — carries a stale token,
  so every write it attempts is rejected and it cancels its own work when the
  next heartbeat fails.

The output artifact itself does not need fencing: output keys are
deterministic and every attempt transcodes the same source, so a late
overwrite of the final key is byte-equivalent in content. The job record in
DynamoDB is the single authority on completion.

## Retry Policy

- `InterruptedError` (shutdown, lease loss) never consumes an attempt verdict:
  the job goes back to `INTERRUPTED` and the message is released immediately.
- Any other failure marks the job `INTERRUPTED` for redelivery until
  `max_attempts` (default 3) is reached, at which point the job is `FAILED`
  terminally. The SQS redrive policy (`maxReceiveCount` 5) dead-letters
  messages as a backstop.

## Reconciliation Sweeper

Queues guarantee delivery of messages that exist — not that a message was ever
produced. The API runs a background reconciler that closes every stuck-state
hole:

- `UPLOADING` older than `stale_uploading_seconds`: if the source object
  exists in S3, the event was lost — transition to `QUEUED` and inject a
  synthetic requeue message. If the object never arrives within
  `upload_expiry_seconds`, fail the job.
- `PROCESSING` with an expired lease and no recent progress: reset to
  `INTERRUPTED` and requeue. The conditional write requires the lease to be
  expired, so an active worker can never be preempted.
- `QUEUED`/`INTERRUPTED` idle past `stale_queued_seconds` (message lost or
  dead-lettered): requeue, bumping `updated_at` so a single sweep does not
  resend every cycle.

All sweeper transitions use the same conditional writes as workers, so races
between the sweeper and a live worker always resolve in the worker's favor.

## Canonical Job State Machine

`CREATED` -> `UPLOADING` -> `QUEUED` -> `PROCESSING` -> `COMPLETED`

Retry paths:

- `PROCESSING` -> `INTERRUPTED` -> `QUEUED` -> `PROCESSING`
- `PROCESSING` -> `FAILED`

State semantics:

- `CREATED`: Job row exists but the upload session is not fully prepared.
- `UPLOADING`: Upload instructions have been issued and the system is waiting for the source object.
- `QUEUED`: Upload completion has been observed and the job is eligible for processing.
- `PROCESSING`: A worker holds the lease for the current attempt (`lease_owner`, `lease_expires_at`).
- `INTERRUPTED`: The attempt ended without a terminal verdict — shutdown, lease loss, or a retryable failure below the attempt cap.
- `FAILED`: The job has exceeded retry policy or encountered a terminal validation/processing error.
- `COMPLETED`: The final artifact exists at the deterministic output key and client-visible processing is finished.

## Consistency Model And Guarantees

Elastic v1 makes the following guarantees:

- Job state is durable once written to DynamoDB.
- Upload and processing are decoupled through queueing.
- S3 and SQS duplicate delivery is tolerated.
- Worker processing is at-least-once, not exactly once.
- Exactly one worker holds a job's lease at any moment; a stale owner is fenced out of every write by its attempt token.
- A worker crash — graceful or not — never strands a job: lease expiry plus the reconciler guarantee another attempt.
- Final client-visible completion is idempotent from the perspective of job state and output key.
- If retryable failures stop occurring, the system should eventually complete the job.

Elastic v1 does not guarantee:

- Exactly-once FFmpeg execution
- Resume-from-checkpoint transcoding after mid-job interruption
- Ordered delivery of S3 notifications
- Infinite retries

## Failure Model

The design must explicitly tolerate the following cases:

### Duplicate Or Reordered Object Events

S3 event notifications are at-least-once and may not arrive in perfect order. Workers must treat every message as potentially duplicated and validate current job state before moving it forward.

### Duplicate SQS Delivery

A message may be delivered more than once, especially near visibility timeout boundaries. Conditional writes and deterministic output keys prevent duplicate delivery from causing conflicting results.

### Worker Crash Or Pod Eviction

If a worker dies before deleting the message, the message becomes visible again after the visibility timeout. The next worker finds the job `PROCESSING` with an expired lease and steals it with a fenced claim. A duplicate delivery while the owner is alive is left untouched — neither deleted (that would destroy the crash-recovery path) nor released (that would hot-loop).

### Zombie Workers

A worker that is paused, partitioned, or resumed after its lease was stolen must not corrupt the new owner's attempt. Its attempt token is stale, so its heartbeats and finalization writes are all rejected; the lease-loss signal also cancels its running FFmpeg.

### Lost Or Consumed Events

If the S3 notification never reaches SQS, or a message is dead-lettered before the job finishes, the reconciler detects the stale job state and injects a synthetic requeue message. No job stays stuck in a non-terminal state indefinitely.

### Spot Interruption

The worker must trap `SIGTERM`, stop FFmpeg, persist the interruption state, and avoid deleting the queue message. The system is considered healthy if a later worker attempt can finish the job safely.

### Partial Output Artifacts

Temporary files must be written to attempt-specific local paths. Final output must only be published to the canonical final key after successful transcode completion.

### Conditional Write Contention

If a worker cannot acquire the correct state transition, it must stop work on that message and avoid clobbering existing state.

## Storage And Naming Conventions

Canonical keys:

- Input: `inputs/{job_id}/source`
- Final output: `outputs/{job_id}/final/1080p.mp4`

Attempt-specific temporary locations:

- Local temp file: `/tmp/elastic/{job_id}/attempt-{attempt}/1080p.mp4`
- Optional scratch S3 key if needed later: `outputs/{job_id}/attempt-{attempt}/1080p.mp4`

The final output key must remain stable across retries so that the client-visible contract does not change between attempts.

## Observability

Logs are structured (JSON when `ELASTIC_LOG_JSON=true`, the default in
Kubernetes) and carry `job_id`, `attempt`, `worker_id`, and `sqs_message_id`
where applicable, so one job's history can be reconstructed across every pod
that touched it.

Prometheus metrics are exported by the worker on port 9100 (when
`ELASTIC_METRICS_ENABLED=true`) and by the API at `/metrics`:

- `elastic_jobs_completed_total` / `elastic_jobs_failed_total` / `elastic_jobs_interrupted_total`
- `elastic_lease_claims_total` / `elastic_lease_steals_total` / `elastic_lease_renewal_failures_total`
- `elastic_visibility_extensions_total`
- `elastic_transcode_duration_seconds` (histogram)
- `elastic_reconciler_requeued_total{reason}` / `elastic_reconciler_expired_total`

Queue depth and worker replica count come from SQS/KEDA rather than the app.

## Local And Cloud Environments

### Local Development

- Dockerized API and worker
- LocalStack for S3, SQS, and DynamoDB
- Local Kubernetes cluster for worker scale behavior if feasible
- Smaller video fixtures for fast iteration

### AWS Deployment

- S3 for real object storage
- SQS Standard for durable buffering
- DynamoDB for job state
- EKS for worker execution
- KEDA for autoscaling
- Spot-backed nodes for the resilience demo

The same high-level data flow should exist in both environments even if local tooling is simplified.
