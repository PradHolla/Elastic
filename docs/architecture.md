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
- Generates presigned upload instructions
- Returns deterministic object keys to the client
- Exposes job status from DynamoDB

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

- Stores canonical job status
- Stores attempt count and last error
- Prevents conflicting transitions through conditional writes

### EKS Worker Pods

- Consume SQS messages
- Normalize incoming event payload into the internal job message shape
- Claim a job only if current state permits processing
- Download the source file, run FFmpeg, upload the output, and finalize status
- Extend visibility timeout while long-running work is in progress
- Handle `SIGTERM` by stopping FFmpeg, marking the job `INTERRUPTED`, and leaving the message unacknowledged

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

## Canonical Job State Machine

`CREATED` -> `UPLOADING` -> `QUEUED` -> `PROCESSING` -> `COMPLETED`

Retry paths:

- `PROCESSING` -> `INTERRUPTED` -> `QUEUED` -> `PROCESSING`
- `PROCESSING` -> `FAILED`

State semantics:

- `CREATED`: Job row exists but the upload session is not fully prepared.
- `UPLOADING`: Upload instructions have been issued and the system is waiting for the source object.
- `QUEUED`: Upload completion has been observed and the job is eligible for processing.
- `PROCESSING`: A worker currently owns the attempt.
- `INTERRUPTED`: The active worker was terminated or otherwise stopped before completion.
- `FAILED`: The job has exceeded retry policy or encountered a terminal validation/processing error.
- `COMPLETED`: The final artifact exists at the deterministic output key and client-visible processing is finished.

## Consistency Model And Guarantees

Elastic v1 makes the following guarantees:

- Job state is durable once written to DynamoDB.
- Upload and processing are decoupled through queueing.
- S3 and SQS duplicate delivery is tolerated.
- Worker processing is at-least-once, not exactly once.
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

If a worker dies before deleting the message, the message should become visible again after visibility timeout and be retried by another worker.

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

## Observability Defaults

Every log line and state transition should include:

- `job_id`
- `attempt`
- `sqs_message_id`
- `worker_id` or pod name
- current state and target state

Minimum metrics to emit later during implementation:

- queue depth
- active workers
- transcode duration
- visibility extensions performed
- retry count
- completed jobs
- interrupted jobs

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
