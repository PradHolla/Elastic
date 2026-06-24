# MVP Specification

## Goal

Build the smallest version of Elastic that still proves the core backend story:

- uploads bypass the API
- work is driven by durable queueing
- workers scale on demand
- interruption does not lose the job
- the client sees coherent job state throughout processing
- the dashboard can create jobs and monitor them without proxying video bytes through the backend

## Target Repo Structure

The repo should evolve into this layout after implementation starts:

```text
.
├── README.md
├── docs/
│   ├── architecture.md
│   └── mvp.md
├── apps/
│   ├── api/
│   ├── worker/
│   └── web/
└── infra/
```

The repo started with the design packet and now grows toward the first buildable MVP. This document remains the implementation contract for the target shape of the system.

## Public API Contract

### `POST /jobs`

Creates a new job record and returns upload instructions.

Request body:

```json
{
  "filename": "sample.mov",
  "content_type": "video/quicktime",
  "size_bytes": 73400320,
  "preset": "1080p"
}
```

Rules:

- `preset` is required and must equal `1080p` in v1.
- `size_bytes` is client-declared metadata and may be used for validation or observability.
- Local MVP may use a single presigned `PUT` upload for smaller fixtures.
- Production-grade multipart upload is a future upgrade required before claiming proven multi-GB support.

Successful response:

```json
{
  "job_id": "01JXEXAMPLE1234567890",
  "status": "UPLOADING",
  "preset": "1080p",
  "input_bucket": "elastic-inputs",
  "input_key": "inputs/01JXEXAMPLE1234567890/source",
  "output_key": "outputs/01JXEXAMPLE1234567890/final/1080p.mp4",
  "upload": {
    "method": "PUT",
    "url": "https://...",
    "headers": {
      "Content-Type": "video/quicktime"
    },
    "expires_in_seconds": 900
  }
}
```

Behavior:

- Create the job record in DynamoDB.
- Generate deterministic object keys.
- Persist `status=UPLOADING` before returning success.
- Return `201 Created`.

### `GET /jobs`

Returns the most recent jobs for the dashboard.

Query parameters:

- `limit` defaults to `20`
- the implementation may cap the maximum page size for safety

Successful response:

```json
[
  {
    "job_id": "01JXEXAMPLE1234567890",
    "status": "PROCESSING",
    "preset": "1080p",
    "attempt_count": 2,
    "input_key": "inputs/01JXEXAMPLE1234567890/source",
    "output_key": "outputs/01JXEXAMPLE1234567890/final/1080p.mp4",
    "last_error": null,
    "created_at": "2026-05-23T12:00:00Z",
    "updated_at": "2026-05-23T12:15:00Z"
  }
]
```

Behavior:

- Return jobs in reverse chronological order by creation time.
- This endpoint powers the dashboard list view and monitoring cards.

### `GET /jobs/{job_id}`

Returns the client-visible state of a job.

Successful response:

```json
{
  "job_id": "01JXEXAMPLE1234567890",
  "status": "PROCESSING",
  "preset": "1080p",
  "attempt_count": 2,
  "input_key": "inputs/01JXEXAMPLE1234567890/source",
  "output_key": "outputs/01JXEXAMPLE1234567890/final/1080p.mp4",
  "last_error": null,
  "created_at": "2026-05-23T12:00:00Z",
  "updated_at": "2026-05-23T12:15:00Z"
}
```

Behavior:

- Return `404` if `job_id` is unknown.
- Expose the latest durable DynamoDB view.
- Do not infer progress from queue state or pod state directly.

## Dashboard Contract

The web app should:

- create a job with `POST /jobs`
- upload the selected file directly to the returned presigned S3 URL
- poll `GET /jobs` and `GET /jobs/{job_id}` to refresh state
- display the latest known job status, attempt count, timestamps, and error text

The dashboard may use a development proxy for `/api` locally and an in-cluster nginx proxy or ingress path in Kubernetes.

## Internal Queue Message Contract

Workers should normalize each S3/SQS event into this canonical internal shape before processing:

```json
{
  "job_id": "01JXEXAMPLE1234567890",
  "bucket": "elastic-inputs",
  "object_key": "inputs/01JXEXAMPLE1234567890/source",
  "size_bytes": 73400320,
  "preset": "1080p",
  "attempt": 1,
  "event_metadata": {
    "sqs_message_id": "8d4d...",
    "s3_event_name": "ObjectCreated:Put",
    "s3_sequencer": "0066...",
    "event_time": "2026-05-23T12:05:00Z"
  }
}
```

Rules:

- `job_id` is derived from the deterministic key, not trusted from arbitrary client input.
- `attempt` is the attempt being started and should match the value written to DynamoDB when the claim succeeds.
- `event_metadata` exists for deduplication diagnostics and traceability.

## DynamoDB Record Shape

The DynamoDB jobs table should contain at least:

```json
{
  "job_id": "01JXEXAMPLE1234567890",
  "status": "PROCESSING",
  "preset": "1080p",
  "input_bucket": "elastic-inputs",
  "input_key": "inputs/01JXEXAMPLE1234567890/source",
  "output_key": "outputs/01JXEXAMPLE1234567890/final/1080p.mp4",
  "attempt_count": 2,
  "last_error": null,
  "created_at": "2026-05-23T12:00:00Z",
  "updated_at": "2026-05-23T12:15:00Z"
}
```

Implementation rules:

- `job_id` is the primary key.
- `attempt_count` is monotonic.
- All state transitions use conditional writes.
- Only one worker may successfully transition a job into `PROCESSING` for a given attempt.

## State Machine

Canonical states:

- `CREATED`
- `UPLOADING`
- `QUEUED`
- `PROCESSING`
- `INTERRUPTED`
- `FAILED`
- `COMPLETED`

Allowed transitions:

- `CREATED -> UPLOADING`
- `UPLOADING -> QUEUED`
- `QUEUED -> PROCESSING`
- `PROCESSING -> INTERRUPTED`
- `PROCESSING -> FAILED`
- `PROCESSING -> COMPLETED`
- `INTERRUPTED -> QUEUED`

Implementation defaults:

- `POST /jobs` should complete the `CREATED -> UPLOADING` transition before returning.
- A worker receiving an upload-complete event should first ensure the job becomes `QUEUED`.
- If the worker can claim immediately, `QUEUED` may be very short-lived.
- A duplicate event for a `COMPLETED` or `FAILED` job should be logged and ignored.

## Worker Contract

The worker implementation must follow this order:

1. Receive a queue message.
2. Normalize the raw event into the canonical internal message shape.
3. Validate that the job exists and the input key matches the expected deterministic key.
4. Move the job into `QUEUED` if the current state still reflects an upload waiting to be processed.
5. Claim the job with a conditional update to `PROCESSING` and increment `attempt_count`.
6. Start a heartbeat loop that extends SQS visibility timeout during processing.
7. Download the source object and transcode it to an attempt-specific temp path.
8. On success, upload to the final output key and transition the job to `COMPLETED`.
9. Delete the SQS message only after the final state transition succeeds.

`SIGTERM` behavior:

- Stop the FFmpeg subprocess.
- Mark the job `INTERRUPTED` if it is still owned by the current attempt.
- Do not delete the SQS message.
- Exit quickly enough for Kubernetes termination windows.

Retry behavior:

- Retries are driven by SQS redelivery.
- The worker should not synthesize new retry messages in v1.
- Terminal validation or unsupported-input errors should transition to `FAILED`.

## Milestones

### Milestone 1: Design Packet

Deliverables:

- `README.md`
- `docs/architecture.md`
- `docs/mvp.md`

Done when:

- The architecture, guarantees, and non-goals are documented clearly.
- API and worker behavior are specific enough for another engineer to implement.

### Milestone 2: Local Happy Path

Deliverables:

- FastAPI service
- Worker service
- Browser dashboard
- Docker-based local stack
- One sample video fixture

Done when:

- A created job reaches `COMPLETED`.
- The output artifact is playable.
- `GET /jobs/{job_id}` reflects the lifecycle coherently.
- The dashboard can create a job, upload a file, and see the resulting state update.

### Milestone 3: Local Failure Validation

Done when:

- Duplicate events do not create conflicting final state.
- Visibility timeout extension prevents healthy long jobs from premature redelivery.
- Forced worker termination results in retry and eventual completion.

### Milestone 4: AWS Thin Slice

Done when:

- The same logical flow works on real S3, SQS, DynamoDB, and EKS.
- KEDA scales the worker deployment from zero based on queue depth.

### Milestone 5: Portfolio Demo

Done when:

- A job is actively processing.
- A pod or Spot-backed node is terminated.
- The job becomes available again and later completes successfully.

## Acceptance Test Scenarios

### Happy Path

- Create a job.
- Upload a valid video.
- Confirm the job moves to `COMPLETED`.
- Confirm the final output exists at the deterministic key.

### Duplicate Event Tolerance

- Re-deliver the same logical upload event.
- Confirm no conflicting final state is written.
- Confirm the client still sees one stable completed artifact.

### Visibility Extension

- Use a transcode long enough to exceed the base visibility timeout.
- Confirm the worker extends visibility and the message is not processed by another worker during healthy execution.

### Interruption Recovery

- Terminate a worker during FFmpeg execution.
- Confirm the worker records `INTERRUPTED` or leaves the job claim recoverable.
- Confirm the message becomes visible again.
- Confirm a later worker completes the job.

### Terminal Failure

- Provide an invalid or unsupported input.
- Confirm the job transitions to `FAILED`.
- Confirm the message is not retried indefinitely.

## Defaults And Assumptions

- The first client is a CLI or script, not a UI.
- The only supported v1 preset is `1080p`.
- Sample files used during early development may be much smaller than true production-sized inputs.
- The MVP is allowed to start with presigned single-object upload locally, as long as the docs clearly state that multipart upload is the production path for very large files.
- Step Functions remains a future enhancement and is not part of the first implementation.
